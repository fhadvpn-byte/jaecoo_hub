import argparse
import asyncio
import logging
import os
import shutil
import sqlite3

from telethon import TelegramClient
from telethon.errors import AccessTokenInvalidError

from database_and_ai import analyze_thread_with_ai, init_db, load_config, save_card

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"
_REQUIRED_CARD_FIELDS = (
    "category",
    "car_model",
    "sub_system",
    "title",
    "problem_or_topic",
    "solution_or_summary",
)


def prepare_unlocked_session(src: str = "user_session.session", dst: str = "scraper_runtime.session") -> str:
    """ينسخ الجلسة إلى ملف عمل حتى لا يتعطل السحب إذا كانت الجلسة الأصلية مقفلة."""
    if os.path.exists(src):
        try:
            shutil.copy2(src, dst)
            conn = sqlite3.connect(dst, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.commit()
            conn.close()
            return os.path.splitext(dst)[0]
        except Exception:
            pass
    return os.path.splitext(src)[0]


def _groq_key(cfg: dict) -> str:
    return (cfg.get("groq_api_key") or cfg.get("gemini_api_key") or "").strip()


def _bot_token(cfg: dict) -> str:
    return (cfg.get("bot_token") or cfg.get("telegram_bot_token") or "").strip()


def _wanted_group(cfg: dict) -> str:
    wanted = (cfg.get("tg_group") or "").strip()
    if wanted.startswith("http"):
        wanted = wanted.rstrip("/").split("/")[-1]
    return wanted.lstrip("@").lower()


def _dialog_matches(dialog, wanted: str) -> bool:
    if not (dialog.is_group or dialog.is_channel):
        return False
    if not wanted:
        return True
    name = (dialog.name or "").lower()
    username = (getattr(dialog.entity, "username", None) or "").lower()
    raw_id = str(dialog.id)
    public_id = raw_id[4:] if raw_id.startswith("-100") else raw_id.lstrip("-")
    return wanted in {name, username, raw_id, public_id} or wanted in name


def _worth_archiving(card: dict) -> bool:
    if not isinstance(card, dict):
        return False
    flag = card.get("is_worth_archiving")
    if flag in (False, 0, "0", "false", "False", None):
        return False
    return all(card.get(field) for field in _REQUIRED_CARD_FIELDS)


def _note(logs: list, message: str, level: str = "info") -> None:
    logs.append(message)
    getattr(logger, level)(message)


async def _open_client(cfg: dict, logs: list):
    api_id = int(cfg.get("tg_api_id") or cfg.get("telegram_api_id") or DEFAULT_API_ID)
    api_hash = cfg.get("tg_api_hash") or cfg.get("telegram_api_hash") or DEFAULT_API_HASH
    token = _bot_token(cfg)

    if token:
        session_name = prepare_unlocked_session("jaecoo_bot_session.session", "bot_runtime.session")
        client = TelegramClient(session_name, api_id, api_hash, timeout=30)
        try:
            await client.start(bot_token=token)
            _note(logs, "تم الاتصال عبر توكن البوت.")
            return client, None
        except AccessTokenInvalidError:
            await client.disconnect()
            _note(logs, "توكن البوت مرفوض، سيتم تجربة الجلسة المحفوظة.", "error")

    session_name = prepare_unlocked_session()
    client = TelegramClient(session_name, api_id, api_hash, timeout=30)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        if token:
            return None, "توكن البوت مرفوض، ولا توجد جلسة تلجرام محفوظة. حدّث Bot Token من BotFather ثم أضف البوت إلى القروب."
        return None, "لا توجد جلسة تلجرام جاهزة. أدخل Bot Token في الإعدادات، احفظه، ثم أضف البوت إلى القروب."
    _note(logs, "تم الاتصال عبر الجلسة المحفوظة.")
    return client, None


async def sync_and_analyze(limit: int = 50) -> dict:
    logs = []
    result = {"ok": False, "saved": 0, "scanned": 0, "group": "", "error": None, "logs": logs}
    init_db()
    cfg = load_config()
    groq_key = _groq_key(cfg)
    if not groq_key:
        result["error"] = "مفتاح Groq API غير موجود. أدخله في الإعدادات ثم احفظ."
        _note(logs, result["error"], "error")
        return result

    try:
        client, error = await _open_client(cfg, logs)
    except Exception as exc:
        result["error"] = f"تعذر الاتصال بتلجرام: {exc}"
        _note(logs, result["error"], "error")
        return result
    if error:
        result["error"] = error
        _note(logs, error, "error")
        return result

    try:
        wanted = _wanted_group(cfg)
        target = None
        fallback = None
        named = None
        async for dialog in client.iter_dialogs(limit=80):
            if not (dialog.is_group or dialog.is_channel):
                continue
            if wanted and _dialog_matches(dialog, wanted):
                target = dialog
                break
            if fallback is None:
                fallback = dialog
            title = (dialog.name or "").lower()
            if named is None and ("jaecoo" in title or "جايكو" in title):
                named = dialog
        if target is None and not wanted:
            target = named or fallback

        if target is None:
            result["error"] = "لم يتم العثور على القروب. أضف البوت إلى المجموعة أو اكتب معرفها في الإعدادات."
            _note(logs, result["error"], "error")
            return result

        result["group"] = target.name or str(target.id)
        _note(logs, f"سحب آخر {limit} رسالة من: {result['group']}")
        messages = await client.get_messages(target, limit=limit)
        chat_id = str(target.id)
        public_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")

        for message in reversed(list(messages)):
            text = (message.text or "").strip()
            if len(text) <= 15:
                continue
            result["scanned"] += 1
            try:
                sender = await message.get_sender()
                author_name = getattr(sender, "first_name", None) or "عضو"
            except Exception:
                author_name = "عضو"

            try:
                card = analyze_thread_with_ai(f"محتوى الرسالة: {text}", groq_key)
            except Exception as exc:
                _note(logs, f"تخطي الرسالة {message.id}: {exc}", "error")
                continue

            if not _worth_archiving(card):
                continue

            thread_url = f"https://t.me/c/{public_id}/{message.id}"
            save_card(card, message.id, thread_url, author_name, text)
            result["saved"] += 1
            _note(logs, f"تم حفظ بطاقة: [{card.get('category')}] {card.get('title')}")

        result["ok"] = True
        _note(logs, f"اكتمل السحب. الرسائل المفحوصة: {result['scanned']}، البطاقات المحفوظة: {result['saved']}")
        return result
    except Exception as exc:
        result["error"] = f"تعذر سحب الرسائل: {exc}"
        _note(logs, result["error"], "error")
        return result
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def run_sync(limit: int = 50) -> dict:
    """نقطة التشغيل التي يستدعيها زر Streamlit دون فتح الطرفية."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(sync_and_analyze(limit=limit))

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(sync_and_analyze(limit=limit))).result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mode", type=str, default="history")
    args, _ = parser.parse_known_args()
    summary = run_sync(limit=args.limit)
    if not summary["ok"]:
        raise SystemExit(summary["error"] or "فشل السحب")
