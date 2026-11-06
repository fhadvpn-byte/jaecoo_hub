import argparse
import asyncio
import logging
import os
import re
import shutil
import sqlite3

from telethon import TelegramClient
from telethon.sessions import StringSession

from database_and_ai import (
    analyze_image_bytes,
    analyze_thread_with_ai,
    compress_image_to_b64,
    get_db_connection,
    init_db,
    load_config,
    save_card,
    save_config,
    transcribe_media_bytes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PERSONAL_SESSION_FILE = os.path.join(PROJECT_DIR, "user_session.session")
RUNTIME_SESSION_FILE = os.path.join(PROJECT_DIR, f"scraper_runtime_{os.getpid()}.session")
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


def prepare_unlocked_session(src: str = PERSONAL_SESSION_FILE, dst: str = RUNTIME_SESSION_FILE) -> str:
    """ينسخ الجلسة الشخصية إلى ملف عمل خاص بهذه العملية حتى لا يتصادم السحب التلقائي."""
    if os.path.exists(src):
        try:
            src_conn = sqlite3.connect(src, timeout=30)
            dst_conn = sqlite3.connect(dst, timeout=30)
            with dst_conn:
                src_conn.backup(dst_conn)
            src_conn.close()
            dst_conn.close()
            return os.path.splitext(dst)[0]
        except Exception:
            try:
                shutil.copy2(src, dst)
                return os.path.splitext(dst)[0]
            except Exception:
                pass
    return os.path.splitext(src)[0]


def _groq_key(cfg: dict) -> str:
    return (cfg.get("groq_api_key") or cfg.get("gemini_api_key") or "").strip()


def _api_credentials(cfg: dict) -> tuple[int, str]:
    api_id = str(cfg.get("tg_api_id") or cfg.get("telegram_api_id") or DEFAULT_API_ID).strip()
    api_hash = (cfg.get("tg_api_hash") or cfg.get("telegram_api_hash") or DEFAULT_API_HASH).strip()
    return int(api_id), api_hash


def _session_string(cfg: dict) -> str:
    for value in (
        cfg.get("tg_session"),
        cfg.get("telegram_session"),
        os.environ.get("TELEGRAM_SESSION"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _remember_session_string(client: TelegramClient, cfg: dict) -> None:
    try:
        exported = StringSession.save(client.session)
    except Exception:
        return
    if exported and exported != _session_string(cfg):
        cfg["tg_session"] = exported
        save_config(cfg)


def _persist_personal_session() -> None:
    if not os.path.exists(RUNTIME_SESSION_FILE):
        return
    if os.path.abspath(RUNTIME_SESSION_FILE) == os.path.abspath(PERSONAL_SESSION_FILE):
        return
    shutil.copy2(RUNTIME_SESSION_FILE, PERSONAL_SESSION_FILE)
    try:
        os.remove(RUNTIME_SESSION_FILE)
    except Exception:
        pass


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


_EMPTY_MARKERS = {"", "nan", "none", "null", "n/a", "na", "undefined"}


def _clean_text(value) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() in _EMPTY_MARKERS:
        return ""
    return text


def _has_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))


def _message_is_pending(message) -> bool:
    if len((message.text or "").strip()) > 15:
        return True
    return any(getattr(message, name, None) for name in ("photo", "voice", "audio", "video", "video_note"))


def _media_label(media_type: str) -> str:
    return {"image": "الصورة", "audio": "المقطع الصوتي", "video": "الفيديو"}.get(media_type, "الرسالة")


def _preview_b64(image_bytes: bytes | None) -> str:
    if not image_bytes:
        return ""
    try:
        return compress_image_to_b64(image_bytes)
    except Exception:
        return ""


async def _extract_message_content(tg_client, groq_key: str, message) -> tuple[str, str, str]:
    """يعيد النص ونوع الوسيط ونسخة الصورة المضغوطة."""
    caption = (message.message or message.text or "").strip()
    if getattr(message, "photo", None):
        image_bytes = await tg_client.download_media(message, file=bytes)
        preview = _preview_b64(image_bytes)
        if image_bytes:
            vision_text = analyze_image_bytes(groq_key, image_bytes, caption)
            return f"محتوى الصورة: {vision_text}\nتعليق العضو: {caption}".strip(), "image", preview
        return caption, "image", preview

    if getattr(message, "voice", None) or getattr(message, "audio", None):
        audio_bytes = await tg_client.download_media(message, file=bytes)
        if audio_bytes:
            audio_ext = getattr(getattr(message, "file", None), "ext", None) or ".ogg"
            spoken_text = transcribe_media_bytes(groq_key, audio_bytes, ext=audio_ext)
            return f"تفريغ المقطع الصوتي: {spoken_text}\nتعليق مرفق: {caption}".strip(), "audio", ""
        return caption, "audio", ""

    if getattr(message, "video", None) or getattr(message, "video_note", None):
        parts = [f"تعليق الفيديو: {caption}"]
        file_size = getattr(getattr(message, "file", None), "size", 0) or 0
        video_ext = getattr(getattr(message, "file", None), "ext", None) or ".mp4"
        if 0 < file_size <= 20 * 1024 * 1024:
            video_bytes = await tg_client.download_media(message, file=bytes)
            if video_bytes:
                spoken_video = transcribe_media_bytes(groq_key, video_bytes, ext=video_ext)
                parts.append(f"الكلام المسموع في الفيديو: {spoken_video}")
        try:
            thumb_bytes = await tg_client.download_media(message, file=bytes, thumb=-1)
        except Exception:
            thumb_bytes = None
        preview = _preview_b64(thumb_bytes)
        if thumb_bytes:
            thumb_desc = analyze_image_bytes(groq_key, thumb_bytes, caption)
            parts.append(f"المشهد المرئي في الفيديو: {thumb_desc}")
        return "\n".join(part for part in parts if part.strip()), "video", preview

    return caption, "text", ""


def _ignore_source(text: str) -> bool:
    """يتجاوز الرسائل العامة والأسئلة الإنجليزية والنصوص غير الواضحة قبل استدعاء Groq."""
    stripped = text.strip()
    if _has_arabic(stripped):
        return False
    lower_words = stripped.lower().split()
    if "?" in stripped or (lower_words and lower_words[0] in {"what", "why", "how", "when", "where", "who", "can", "does", "do", "is", "are"}):
        return True
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    return len(letters) < 40


def _worth_archiving(card: dict) -> bool:
    if not isinstance(card, dict):
        return False
    flag = card.get("is_worth_archiving")
    if flag in (False, 0, "0", "false", "False", None):
        return False
    if _clean_text(card.get("category")) == "نبض القروب والدردشة العامة":
        return False
    cleaned = {field: _clean_text(card.get(field)) for field in _REQUIRED_CARD_FIELDS}
    if not all(cleaned.values()):
        return False
    return all(_has_arabic(cleaned[field]) for field in ("title", "problem_or_topic", "solution_or_summary"))


def _note(logs: list, message: str, level: str = "info") -> None:
    logs.append(message)
    getattr(logger, level)(message)


def _report(on_progress, message: str, fraction: float) -> None:
    if on_progress is None:
        return
    try:
        on_progress(message, max(0.0, min(1.0, float(fraction))))
    except Exception:
        pass


async def _connect_saved_client(client: TelegramClient, logs: list, message: str) -> bool:
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return False
    _note(logs, message)
    return True


def _stored_card_count() -> int:
    conn = get_db_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0])
    finally:
        conn.close()


async def _open_client(cfg: dict, logs: list):
    """يفضل ملف الجلسة المحلي مباشرة، ثم الجلسة النصية إن لم يوجد الملف."""
    api_id, api_hash = _api_credentials(cfg)
    if os.path.exists(PERSONAL_SESSION_FILE):
        session_name = prepare_unlocked_session(PERSONAL_SESSION_FILE, RUNTIME_SESSION_FILE)
        client = TelegramClient(session_name, api_id, api_hash, timeout=30)
        if await _connect_saved_client(client, logs, "تم الاتصال بملف الجلسة المحلي user_session.session."):
            return client, None

    session_text = _session_string(cfg)
    if session_text:
        try:
            client = TelegramClient(StringSession(session_text), api_id, api_hash, timeout=30)
        except Exception:
            client = None
        if client is not None and await _connect_saved_client(client, logs, "تم الاتصال بجلسة الحساب النصية."):
            return client, None

    return None, "تعذر الاتصال بجلسة تيليجرام."


async def sync_and_analyze(limit: int = 50, on_progress=None) -> dict:
    logs = []
    result = {"ok": False, "saved": 0, "scanned": 0, "group": "", "error": None, "logs": logs}
    init_db()
    cfg = load_config()
    groq_key = _groq_key(cfg)
    if not groq_key:
        result["error"] = "مفتاح Groq API غير موجود. أدخله في الإعدادات ثم احفظ."
        _note(logs, result["error"], "error")
        return result

    _report(on_progress, "جاري الاتصال ب تيليجرام...", 0.08)
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
            result["error"] = "لم يتم العثور على القروب ضمن الجلسة الشخصية. اكتب معرف القروب في الإعدادات."
            _note(logs, result["error"], "error")
            return result

        result["group"] = target.name or str(target.id)
        _report(on_progress, "جاري سحب الرسائل...", 0.32)
        _note(logs, f"سحب آخر {limit} رسالة من: {result['group']}")
        messages = await client.get_messages(target, limit=limit)
        chat_id = str(target.id)
        public_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")
        pending = [message for message in reversed(list(messages)) if _message_is_pending(message)]
        total = max(len(pending), 1)
        _report(
            on_progress,
            f"تم سحب {len(messages)} رسالة من تيليجرام، ومنها {len(pending)} رسالة جاهزة للتحليل.",
            0.36,
        )

        for index, message in enumerate(pending, start=1):
            step = 0.4 + 0.55 * ((index - 1) / total)
            result["scanned"] += 1
            try:
                sender = await message.get_sender()
                author_name = getattr(sender, "first_name", None) or "عضو"
            except Exception:
                author_name = "عضو"

            try:
                text, media_type, media_b64 = await _extract_message_content(client, groq_key, message)
            except Exception as exc:
                _note(logs, f"تخطي الرسالة {message.id}: {exc}", "error")
                _report(on_progress, f"الرسالة {index} من {len(pending)}: تعذر قراءة وسائطها.", step + (0.2 / total))
                continue

            if media_type == "text" and _ignore_source(text):
                _report(
                    on_progress,
                    f"الرسالة {index} من {len(pending)}: تم تجاهلها لأنها عامة أو غير واضحة.",
                    step + (0.2 / total),
                )
                continue
            if len(text.strip()) < 15:
                _report(on_progress, f"الرسالة {index} من {len(pending)}: لا تحتوي محتوى كافياً للحفظ.", step + (0.2 / total))
                continue

            _report(
                on_progress,
                f"جاري تحليل {_media_label(media_type)} {index} من {len(pending)} عبر Groq...",
                min(0.97, step + (0.2 / total)),
            )
            try:
                card = analyze_thread_with_ai(f"محتوى الرسالة: {text}", groq_key)
            except Exception as exc:
                _note(logs, f"تخطي الرسالة {message.id}: {exc}", "error")
                _report(on_progress, f"الرسالة {index} من {len(pending)}: تعذر تحليلها عبر Groq.", step + (0.4 / total))
                continue

            if not _worth_archiving(card):
                _report(on_progress, f"الرسالة {index} من {len(pending)}: اكتمل تحليل Groq ولم تُحفظ بطاقة.", step + (0.45 / total))
                continue

            thread_url = f"https://t.me/c/{public_id}/{message.id}"
            save_card(card, message.id, thread_url, author_name, text, media_type, media_b64)
            result["saved"] += 1
            _note(logs, f"تم حفظ بطاقة: [{card.get('category')}] {card.get('title')}")
            _report(
                on_progress,
                f"تم حفظ البطاقة بنجاح. عدد البطاقات في القاعدة: {_stored_card_count()}.",
                min(0.99, step + (0.45 / total)),
            )

        result["ok"] = True
        _report(on_progress, "اكتمل السحب والتحليل.", 1.0)
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
        try:
            _persist_personal_session()
        except Exception:
            pass


def run_sync(limit: int = 50, on_progress=None) -> dict:
    """نقطة التشغيل التي يستدعيها زر Streamlit دون فتح الطرفية."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(sync_and_analyze(limit=limit, on_progress=on_progress))

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(sync_and_analyze(limit=limit, on_progress=on_progress))).result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mode", type=str, default="history")
    args, _ = parser.parse_known_args()
    summary = run_sync(limit=args.limit)
    if not summary["ok"]:
        raise SystemExit(summary["error"] or "فشل السحب")
