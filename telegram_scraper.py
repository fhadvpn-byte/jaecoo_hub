import argparse
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

from database_and_ai import (
    analyze_image_bytes,
    analyze_thread_with_ai,
    collect_groq_keys,
    compress_image_to_b64,
    extract_urls,
    get_db_connection,
    init_db,
    is_car_related_thread,
    is_dealer_or_maintenance_msg,
    load_telegram_session,
    mark_telegram_session_invalid,
    save_telegram_session,
    set_app_setting,
    is_owner_experience_msg,
    is_product_link_text,
    load_config,
    save_card,
    save_config,
    transcribe_media_bytes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
_CLIENT_LOCK = threading.Lock()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PERSONAL_SESSION_FILE = os.path.join(PROJECT_DIR, "user_session.session")
RUNTIME_SESSION_FILE = os.path.join(PROJECT_DIR, f"scraper_runtime_{os.getpid()}.session")
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"
START_SYNC_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SYNC_GROUP_KEY = "jaecoo_2026"
SYNC_CHAIN_BATCH = 500
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
    stored = load_telegram_session()
    if stored:
        return stored
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
    """لا يعيد إنشاء user_session.session. الجلسة المعتمدة نصية وتُحفظ في قاعدة البيانات."""
    if not os.path.exists(RUNTIME_SESSION_FILE):
        return
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
    text = (message.text or "").strip()
    if is_dealer_or_maintenance_msg(text):
        return True
    if len(text) > 15:
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


def _url_note(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        return ""
    return f"\n[روابط مرفقة في الرسالة: {' | '.join(urls)}]"


def _message_is_after_start(message) -> bool:
    msg_date = getattr(message, "date", None)
    if msg_date is None:
        return True
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    return msg_date >= START_SYNC_DATE


async def _extract_message_content(tg_client, groq_key: str, message) -> tuple[str, str, str]:
    """يعيد النص ونوع الوسيط ونسخة الصورة المضغوطة."""
    caption = (message.message or message.text or "").strip()
    url_note = _url_note(caption)
    caption_for_ai = f"{caption}{url_note}".strip()
    kind = detect_msg_media_kind(message)
    file_ext = getattr(getattr(message, "file", None), "ext", None) or ""
    if kind == "image":
        image_bytes = await tg_client.download_media(message, file=bytes)
        preview = _preview_b64(image_bytes)
        if image_bytes:
            mime = "image/jpeg"
            document = getattr(message, "document", None)
            if document and getattr(document, "mime_type", None):
                mime = document.mime_type
            vision_text = analyze_image_bytes(groq_key, image_bytes, caption_for_ai, mime)
            return f"{vision_text}\nالنص والروابط: {caption}{url_note}".strip(), "image", preview
        return f"{caption}{url_note}".strip(), "image", preview

    if kind == "audio":
        audio_bytes = await tg_client.download_media(message, file=bytes)
        if audio_bytes:
            spoken_text = transcribe_media_bytes(groq_key, audio_bytes, ext=file_ext or ".ogg")
            return f"[مقطع صوتي مفرغ نصياً]: {spoken_text}\nتعليق: {caption}{url_note}".strip(), "audio", ""
        return f"{caption}{url_note}".strip(), "audio", ""

    if kind == "video":
        parts = [f"تعليق الفيديو: {caption}{url_note}"] if caption or url_note else []
        try:
            thumb_bytes = await tg_client.download_media(message, file=bytes, thumb=-1)
        except Exception:
            thumb_bytes = None
        preview = _preview_b64(thumb_bytes)
        if thumb_bytes:
            thumb_desc = analyze_image_bytes(groq_key, thumb_bytes, caption_for_ai, "video/thumb")
            parts.append(f"[المشهد المرئي في الفيديو]: {thumb_desc}")
        file_size = getattr(getattr(message, "file", None), "size", 0) or 0
        if 0 < file_size <= 20 * 1024 * 1024:
            video_bytes = await tg_client.download_media(message, file=bytes)
            if video_bytes:
                spoken_video = transcribe_media_bytes(groq_key, video_bytes, ext=file_ext or ".mp4")
                if spoken_video:
                    parts.append(f"[الكلام المنطوق داخل الفيديو]: {spoken_video}")
        return "\n".join(part for part in parts if part.strip()), "video", preview

    return f"{caption}{url_note}".strip(), "text", ""


def _ignore_source(text: str) -> bool:
    """يتجاوز الرسائل العامة والأسئلة الإنجليزية والنصوص غير الواضحة قبل استدعاء Groq."""
    stripped = text.strip()
    if is_dealer_or_maintenance_msg(stripped) or is_product_link_text(stripped) or is_owner_experience_msg(stripped) or _has_arabic(stripped):
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


def get_last_synced_id(group_key: str = "main_group") -> int:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT last_msg_id FROM sync_state WHERE group_key = ?",
            (group_key,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _set_sync_active(active: bool) -> None:
    try:
        set_app_setting("telegram_sync_active", "1" if active else "0")
    except Exception:
        pass


def force_release_client_lock() -> None:
    """يفك قفل الجلسة العالق ويصفّر حالة الانشغال حتى لو انقطع الاتصال."""
    try:
        _CLIENT_LOCK.release()
    except RuntimeError:
        pass
    _set_sync_active(False)
    try:
        set_app_setting("telegram_sync_attempt_at", "0")
    except Exception:
        pass


def reset_sync_checkpoint(group_key: str = SYNC_GROUP_KEY) -> None:
    """يصفر مؤشر السحب ويفك أي قفل عالق حتى تبدأ الدفعة التالية من 1/1/2026."""
    update_last_synced_id(0, group_key)
    force_release_client_lock()


def update_last_synced_id(msg_id: int, group_key: str = "main_group") -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO sync_state (group_key, last_msg_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(group_key) DO UPDATE SET
                last_msg_id = excluded.last_msg_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_key, int(msg_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _rate_limit_wait_seconds(error: Exception) -> int:
    match = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", str(error))
    if not match:
        return 25
    minutes = int(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return min(1800, int(minutes * 60 + seconds) + 3)


def _is_rate_limit(error: Exception) -> bool:
    text = str(error).lower()
    return "429" in text or "rate_limit" in text


def detect_msg_media_kind(message) -> str:
    """يتعرف على الصور والفيديو والصوت حتى لو أُرسلت كملف."""
    if getattr(message, "photo", None):
        return "image"
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        return "audio"
    if getattr(message, "video", None) or getattr(message, "video_note", None):
        return "video"
    document = getattr(message, "document", None)
    mime = str(getattr(document, "mime_type", "") or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "text"


def _reply_to_id(message) -> int | None:
    reply_id = getattr(message, "reply_to_msg_id", None)
    if not reply_id and getattr(message, "reply_to", None):
        reply_id = getattr(message.reply_to, "reply_to_msg_id", None)
    return int(reply_id) if reply_id else None


def _group_reply_threads(messages: list) -> dict:
    """يربط كل رسالة بالردود التي جاءت عليها داخل الدفعة."""
    threads = {}
    message_to_root = {}
    for message in sorted(messages, key=lambda item: item.id):
        reply_id = _reply_to_id(message)
        if reply_id and reply_id in message_to_root:
            root_id = message_to_root[reply_id]
            threads[root_id].append(message)
            message_to_root[message.id] = root_id
        else:
            threads[message.id] = [message]
            message_to_root[message.id] = message.id
    return threads


def _should_analyze_message(message) -> bool:
    if detect_msg_media_kind(message) != "text":
        return True
    text = (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()
    if is_dealer_or_maintenance_msg(text) or is_product_link_text(text) or is_owner_experience_msg(text):
        return True
    return len(text) >= 15


async def _run_with_backoff(factory, on_progress):
    """لا ينتظر دقيقة امتلاء النموذج. المحاولة التالية بعد 6 ثوانٍ فقط."""
    try:
        value = factory()
        if asyncio.iscoroutine(value):
            return await value
        return value
    except Exception as exc:
        if not _is_rate_limit(exc):
            raise
        _report(on_progress, "رصيد النموذج ممتلئ. انتظار 5 ثوانٍ ثم المتابعة.", 0.45)
        await asyncio.sleep(5)
        value = factory()
        if asyncio.iscoroutine(value):
            return await value
        return value


def _stored_card_count() -> int:
    conn = get_db_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0])
    finally:
        conn.close()


async def _open_client(cfg: dict, logs: list):
    """يفضل String Session المحفوظة، ثم ملف الجلسة إن لم توجد جلسة نصية."""
    api_id, api_hash = _api_credentials(cfg)
    session_text = _session_string(cfg)
    if session_text:
        try:
            client = TelegramClient(StringSession(session_text), api_id, api_hash, timeout=30, receive_updates=False)
        except Exception:
            client = None
        if client is not None:
            try:
                if await _connect_saved_client(client, logs, "تم الاتصال بجلسة الحساب النصية."):
                    set_app_setting("telegram_session_status", "ok")
                    return client, None
            except Exception:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
        mark_telegram_session_invalid()
        return None, "جلسة تيليجرام غير صالحة. افتح قسم تفعيل الجلسة وأدخل رقم الجوال."

    if os.path.exists(PERSONAL_SESSION_FILE):
        session_name = prepare_unlocked_session(PERSONAL_SESSION_FILE, RUNTIME_SESSION_FILE)
        client = TelegramClient(session_name, api_id, api_hash, timeout=30, receive_updates=False)
        try:
            if await _connect_saved_client(client, logs, "تم الاتصال بملف الجلسة المحلي user_session.session."):
                return client, None
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise

    return None, "جلسة تيليجرام غير مفعّلة. افتح قسم تفعيل الجلسة وأدخل رقم الجوال."


async def sync_and_analyze(limit: int = 50, on_progress=None) -> dict:
    logs = []
    result = {"ok": False, "saved": 0, "scanned": 0, "group": "", "error": None, "logs": logs}
    init_db()
    cfg = load_config()
    groq_key = _groq_key(cfg) or (collect_groq_keys() or [""])[0]
    if not collect_groq_keys(groq_key):
        result["error"] = "مفتاح Groq API غير موجود. أدخله في الإعدادات ثم احفظ."
        _note(logs, result["error"], "error")
        return result

    _report(on_progress, "جاري الاتصال ب تيليجرام...", 0.08)
    try:
        client, error = await _open_client(cfg, logs)
    except Exception as exc:
        result["error"] = _telegram_error_message(exc)
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
        chat_id = str(target.id)
        public_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")
        group_key = SYNC_GROUP_KEY
        requested = max(1, int(limit or SYNC_CHAIN_BATCH))

        def _span(done_units: float) -> float:
            return min(0.98, 0.08 + 0.90 * (done_units / requested))

        last_id = get_last_synced_id(group_key)
        if last_id > 0:
            start_note = (
                f"يكمل السحب من الرسالة رقم {last_id + 1} بعد تاريخ 1/1/2026. "
                f"المطلوب {requested} رسالة على دفعات من {SYNC_CHAIN_BATCH}."
            )
        else:
            start_note = (
                f"يبدأ السحب من تاريخ 1/1/2026. "
                f"المطلوب {requested} رسالة على دفعات من {SYNC_CHAIN_BATCH}."
            )
        _report(on_progress, start_note, 0.08)
        _note(logs, f"متابعة أرشفة {result['group']} بعد الرسالة {last_id}. المطلوب {requested}.")

        pulled = 0
        batch_no = 0
        while pulled < requested:
            batch_no += 1
            last_id = get_last_synced_id(group_key)
            this_limit = min(SYNC_CHAIN_BATCH, requested - pulled)
            batch_base = pulled
            raw_messages = []
            highest_id = last_id
            fetched = 0
            iter_kwargs = {"reverse": True, "limit": this_limit}
            if last_id > 0:
                iter_kwargs["min_id"] = last_id
            else:
                iter_kwargs["offset_date"] = START_SYNC_DATE
            _report(
                on_progress,
                f"الدفعة {batch_no}: سحب {this_limit} رسالة. المفحوص حتى الآن {pulled} من {requested}.",
                _span(batch_base),
            )
            async for message in client.iter_messages(target, **iter_kwargs):
                fetched += 1
                pulled += 1
                highest_id = message.id
                if not _message_is_after_start(message):
                    continue
                result["scanned"] += 1
                fraction = _span(batch_base + fetched * 0.45)
                msg_date = message.date.strftime("%Y-%m-%d") if getattr(message, "date", None) else ""
                _report(
                    on_progress,
                    (
                        f"الدفعة {batch_no}: الرسالة رقم {message.id} | التاريخ {msg_date} | "
                        f"تم فحص {result['scanned']} من {requested} | بطاقات {result['saved']}"
                    ),
                    fraction,
                )
                raw_messages.append(message)
            if fetched == 0 or highest_id <= last_id:
                _report(on_progress, "لا توجد رسائل أحدث بعد نقطة التوقف.", _span(pulled))
                break

            referenced_ids = {reply_id for reply_id in (_reply_to_id(message) for message in raw_messages) if reply_id}
            batch = [
                message for message in raw_messages
                if _should_analyze_message(message) or _reply_to_id(message) or message.id in referenced_ids
            ]

            threads = _group_reply_threads(batch)
            thread_total = max(len(threads), 1)
            for thread_index, (root_id, thread_messages) in enumerate(threads.items(), start=1):
                fraction = _span(batch_base + fetched * (0.45 + 0.55 * thread_index / thread_total))
                blocks = []
                raw_parts = []
                dominant_media = "text"
                best_thumb = ""
                author_name = "عضو"
                for index, message in enumerate(thread_messages):
                    role = "الرسالة الأساسية (طرح المشكلة أو الموضوع)" if index == 0 else f"الرد رقم {index} (الإجابة أو الحل)"
                    if index == 0:
                        try:
                            sender = await message.get_sender()
                            author_name = getattr(sender, "first_name", None) or "عضو"
                        except Exception:
                            author_name = "عضو"
                    try:
                        parsed_text, media_kind, media_b64 = await _run_with_backoff(
                            lambda message=message: _extract_message_content(client, groq_key, message),
                            on_progress,
                        )
                    except Exception as exc:
                        _note(logs, f"تخطي الرسالة {message.id}: {exc}", "error")
                        _report(on_progress, f"الرسالة {message.id}: تعذر قراءة وسائطها.", fraction)
                        continue
                    if media_kind != "text":
                        await asyncio.sleep(1.5)
                    if (parsed_text or "").strip():
                        raw_parts.append(parsed_text.strip())
                        blocks.append(f"--- {role} (نوعها: {media_kind}) ---\n{parsed_text}")
                    if {"text": 0, "image": 1, "audio": 2, "video": 3}[media_kind] > {"text": 0, "image": 1, "audio": 2, "video": 3}[dominant_media]:
                        dominant_media = media_kind
                    if media_b64 and not best_thumb:
                        best_thumb = media_b64

                thread_text = "\n\n".join(blocks).strip()
                content = "\n".join(raw_parts)
                has_product = is_product_link_text(content)
                if _ignore_source(content):
                    continue
                if not has_product and not is_car_related_thread(content, dominant_media):
                    _report(on_progress, f"تخطي النقاش {root_id}: دردشة غير متعلقة بالسيارة.", fraction)
                    continue
                _report(
                    on_progress,
                    f"الدفعة {batch_no}: تحليل النقاش رقم {root_id} عبر Groq | بطاقات {result['saved']}",
                    fraction,
                )
                try:
                    card = await _run_with_backoff(
                        lambda thread_text=thread_text, fraction=fraction: analyze_thread_with_ai(
                            thread_text,
                            groq_key,
                            on_switch=lambda note, fraction=fraction: _report(on_progress, note, fraction),
                        ),
                        on_progress,
                    )
                except Exception as exc:
                    _note(logs, f"تخطي النقاش {root_id}: {exc}", "error")
                    _report(on_progress, f"النقاش {root_id}: تعذر تحليله عبر Groq.", fraction)
                    continue
                await asyncio.sleep(1.5)
                if not _worth_archiving(card):
                    _report(on_progress, f"النقاش {root_id}: لا يحتوي حلاً كافياً لحفظ بطاقة.", fraction)
                    continue
                thread_url = f"https://t.me/c/{public_id}/{root_id}"
                save_card(card, root_id, thread_url, author_name, thread_text, dominant_media, best_thumb)
                result["saved"] += 1
                _note(logs, f"تم حفظ بطاقة: [{card.get('category')}] {card.get('title')}")
                _report(
                    on_progress,
                    f"تم حفظ البطاقة بنجاح. عدد البطاقات في القاعدة: {_stored_card_count()}.",
                    fraction,
                )

            update_last_synced_id(highest_id, group_key)
            _report(
                on_progress,
                f"اكتملت الدفعة {batch_no}. المفحوص {pulled} من {requested}، والبطاقات {result['saved']}.",
                _span(pulled),
            )
            if fetched < this_limit:
                _report(on_progress, "وصل السحب إلى آخر رسائل القروب المتاحة.", _span(pulled))
                break

        result["ok"] = True
        _report(
            on_progress,
            f"اكتمل السحب. تم فحص {result['scanned']} رسالة وإضافة {result['saved']} بطاقة. آخر نقطة: {get_last_synced_id(group_key)}.",
            1.0,
        )
        _note(logs, f"اكتمل السحب. الرسائل المفحوصة: {result['scanned']}، البطاقات المحفوظة: {result['saved']}")
        return result
    except Exception as exc:
        result["error"] = _telegram_error_message(exc)
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


def _login_error_message(exc: Exception) -> str:
    name = exc.__class__.__name__
    if name == "PhoneNumberInvalidError":
        return "رقم الجوال غير صحيح. أدخله بالصيغة الدولية مثل +9665xxxxxxx."
    if name == "PhoneCodeInvalidError":
        return "رمز التحقق غير صحيح."
    if name == "PhoneCodeExpiredError":
        return "انتهت صلاحية الرمز. أعد إرسال رمز التحقق."
    if name == "FloodWaitError":
        seconds = getattr(exc, "seconds", None)
        if seconds:
            return f"تيليجرام طلب الانتظار {seconds} ثانية قبل إعادة المحاولة."
        return "تيليجرام طلب الانتظار قبل إعادة المحاولة."
    if name == "SessionPasswordNeededError":
        return "الحساب يطلب كلمة مرور التحقق بخطوتين."
    return f"تعذر إكمال التفعيل: {exc}"


async def _request_login_code(phone: str) -> dict:
    cfg = load_config()
    api_id, api_hash = _api_credentials(cfg)
    client = TelegramClient(StringSession(), api_id, api_hash, timeout=30, receive_updates=False)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        return {
            "ok": True,
            "phone_code_hash": sent.phone_code_hash,
            "temp_session": client.session.save(),
        }
    except Exception as exc:
        return {"ok": False, "error": _login_error_message(exc)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _confirm_login(phone: str, code: str, phone_code_hash: str, temp_session: str, password: str = "") -> dict:
    cfg = load_config()
    api_id, api_hash = _api_credentials(cfg)
    client = TelegramClient(StringSession(temp_session), api_id, api_hash, timeout=30, receive_updates=False)
    try:
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except Exception as exc:
            if exc.__class__.__name__ != "SessionPasswordNeededError":
                return {"ok": False, "error": _login_error_message(exc), "temp_session": client.session.save()}
            if not password.strip():
                return {
                    "ok": False,
                    "needs_password": True,
                    "error": "الحساب يطلب كلمة مرور التحقق بخطوتين.",
                    "temp_session": client.session.save(),
                }
            await client.sign_in(password=password)
        session_text = client.session.save()
        save_telegram_session(session_text)
        os.environ["TELEGRAM_SESSION"] = session_text
        return {"ok": True, "session": session_text}
    except Exception as exc:
        return {"ok": False, "error": _login_error_message(exc)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _run_login(factory):
    acquired = False
    try:
        acquired = _CLIENT_LOCK.acquire(timeout=20)
        if not acquired:
            return {"ok": False, "error": "توجد محاولة اتصال جارية. انتظر ثواني ثم أعد المحاولة."}
        _set_sync_active(True)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(factory())).result()
    finally:
        if acquired:
            try:
                _CLIENT_LOCK.release()
            except RuntimeError:
                pass
            _set_sync_active(False)


def request_telegram_code(phone: str) -> dict:
    cleaned = phone.strip().replace(" ", "")
    if not cleaned.startswith("+") or not cleaned[1:].isdigit():
        return {"ok": False, "error": "أدخل رقم الجوال بالصيغة الدولية، ويبدأ بعلامة +."}
    return _run_login(lambda: _request_login_code(cleaned))


def confirm_telegram_login(phone: str, code: str, phone_code_hash: str, temp_session: str, password: str = "") -> dict:
    if not code.strip():
        return {"ok": False, "error": "أدخل رمز التحقق الذي وصلك في تيليجرام."}
    if not temp_session or not phone_code_hash:
        return {"ok": False, "error": "أرسل رمز التحقق أولاً."}
    return _run_login(
        lambda: _confirm_login(phone.strip(), code.strip(), phone_code_hash, temp_session, password)
    )


def _telegram_error_message(exc: Exception) -> str:
    text = str(exc)
    if "authorization key" in text.lower() or "AuthKeyDuplicated" in text:
        mark_telegram_session_invalid()
        return (
            "أُلغيت جلسة تيليجرام لأنها فُتحت من أكثر من مكان في الوقت نفسه. "
            "فعّلها من قسم تفعيل الجلسة، ثم انسخ الكود الجديد إلى TELEGRAM_SESSION."
        )
    if text.startswith("تعذر"):
        return text
    return f"تعذر سحب الرسائل: {text}"


def run_sync(limit: int = 50, on_progress=None) -> dict:
    """نقطة التشغيل التي يستدعيها زر Streamlit دون فتح الطرفية."""
    init_db()
    acquired = False
    try:
        acquired = _CLIENT_LOCK.acquire(blocking=False)
        if not acquired:
            return {
                "ok": False,
                "saved": 0,
                "scanned": 0,
                "group": "",
                "error": "توجد عملية سحب جارية حالياً. انتظر حتى تُغلق جلسة تيليجرام.",
                "logs": [],
            }
        _set_sync_active(True)
        set_app_setting("telegram_sync_attempt_at", str(time.time()))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(sync_and_analyze(limit=limit, on_progress=on_progress))

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(sync_and_analyze(limit=limit, on_progress=on_progress))).result()
    finally:
        if acquired:
            try:
                _CLIENT_LOCK.release()
            except RuntimeError:
                pass
            _set_sync_active(False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mode", type=str, default="history")
    args, _ = parser.parse_known_args()
    summary = run_sync(limit=args.limit)
    if not summary["ok"]:
        raise SystemExit(summary["error"] or "فشل السحب")
