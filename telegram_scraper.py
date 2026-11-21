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
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from database_and_ai import (
    analyze_image_bytes,
    analyze_thread_with_ai,
    analyze_threads_batch_with_ai,
    card_rejection_reason,
    collect_groq_keys,
    is_key_or_quota_error,
    compress_image_to_b64,
    describe_groq_error,
    USEFUL_TOPIC_TERMS,
    card_passes_quality_gate,
    extract_urls,
    get_db_connection,
    init_db,
    is_dealer_or_maintenance_msg,
    load_telegram_session,
    mark_telegram_session_invalid,
    save_telegram_session,
    set_app_setting,
    is_owner_experience_msg,
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
SYNC_CHAIN_BATCH = 100
AI_BATCH_SIZE = 8
_STOP_SYNC_EVENT = threading.Event()
_SYNC_ROUND_ACTIVE = False
SESSION_PULL_CAP = 10000
BATCH_PAUSE_SECONDS = 2
AI_REQUEST_PACING_SECONDS = 0.5
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_PAUSE_CAP = 25
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
    return (
        os.environ.get("GROQ_API_KEY")
        or cfg.get("groq_api_key")
        or cfg.get("gemini_api_key")
        or ""
    ).strip()


def _clean_session_token(raw: str) -> str:
    text = str(raw or "").strip()
    if "=" in text and text.upper().startswith("TELEGRAM_SESSION"):
        text = text.split("=", 1)[1].strip()
    quotes = "\"'`“”‘’"
    while len(text) >= 2 and text[0] in quotes and text[-1] in quotes:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _secret_value_from_streamlit(keys: tuple[str, ...]) -> str:
    import sys
    if "streamlit" not in sys.modules:
        return ""
    try:
        import streamlit as st
        secrets_obj = st.secrets
    except Exception:
        return ""
    for key in keys:
        try:
            val = str(secrets_obj.get(key, "") or "").strip()
            if val:
                return val
        except Exception:
            pass
    try:
        sections = list(secrets_obj.keys())
    except Exception:
        sections = []
    for sec in sections:
        try:
            bag = secrets_obj[sec]
            if hasattr(bag, "get"):
                for key in keys:
                    val = str(bag.get(key, "") or "").strip()
                    if val:
                        return val
        except Exception:
            continue
    return ""


def _api_credentials(cfg: dict) -> tuple[int, str]:
    api_id = (
        os.environ.get("TG_API_ID")
        or _secret_value_from_streamlit(("TG_API_ID", "tg_api_id", "telegram_api_id"))
        or cfg.get("tg_api_id")
        or cfg.get("telegram_api_id")
        or DEFAULT_API_ID
    )
    api_hash = (
        os.environ.get("TG_API_HASH")
        or _secret_value_from_streamlit(("TG_API_HASH", "tg_api_hash", "telegram_api_hash"))
        or cfg.get("tg_api_hash")
        or cfg.get("telegram_api_hash")
        or DEFAULT_API_HASH
    )
    return int(str(api_id).strip()), str(api_hash).strip()


def _candidate_sessions(cfg: dict) -> list[str]:
    """يضع جلسة البيئة وSecrets في المرتبة الأولى قبل قاعدة البيانات المحلية."""
    candidates = []
    secret_sess = _secret_value_from_streamlit(
        ("TELEGRAM_SESSION", "tg_session", "telegram_session", "TG_SESSION", "SESSION_STRING")
    )
    for source in (
        os.environ.get("TELEGRAM_SESSION"),
        secret_sess,
        cfg.get("tg_session"),
        cfg.get("telegram_session"),
        load_telegram_session(),
    ):
        cleaned = _clean_session_token(source)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


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


def request_stop_sync() -> None:
    _STOP_SYNC_EVENT.set()


def clear_stop_sync() -> None:
    _STOP_SYNC_EVENT.clear()


def _prepare_sync_round() -> None:
    """يلغي علامة إيقاف عالقة قبل بدء جولة جديدة. الإيقاف أثناء جولة نشطة يبقى سارياً."""
    if not _SYNC_ROUND_ACTIVE:
        clear_stop_sync()


async def _extract_message_content(tg_client, groq_key: str, message, skip_media: bool = False) -> tuple[str, str, str]:
    """يعيد النص ونوع الوسيط ونسخة الصورة المضغوطة."""
    caption = (message.message or message.text or "").strip()
    url_note = _url_note(caption)
    caption_for_ai = f"{caption}{url_note}".strip()
    kind = detect_msg_media_kind(message)
    if skip_media:
        if not caption_for_ai:
            return "", kind, ""
        labels = {"image": "[مرفق صورة]", "audio": "[مرفق صوتي]", "video": "[مرفق فيديو]"}
        prefix = labels.get(kind, "")
        text = f"{prefix} {caption_for_ai}".strip() if prefix else caption_for_ai
        return text, kind, ""
    file_ext = getattr(getattr(message, "file", None), "ext", None) or ""
    if kind == "image":
        try:
            image_bytes = await tg_client.download_media(message, file=bytes)
        except Exception:
            image_bytes = None
        preview = _preview_b64(image_bytes)
        vision_text = ""
        if image_bytes:
            mime = "image/jpeg"
            document = getattr(message, "document", None)
            if document and getattr(document, "mime_type", None):
                mime = document.mime_type
            try:
                vision_text = await asyncio.to_thread(
                    analyze_image_bytes, groq_key, image_bytes, caption_for_ai, mime
                )
            except Exception:
                vision_text = ""
        if vision_text:
            return f"{vision_text}\nالنص والروابط: {caption}{url_note}".strip(), "image", preview
        return f"{caption}{url_note}".strip(), "image", preview

    if kind == "audio":
        spoken_text = ""
        try:
            audio_bytes = await tg_client.download_media(message, file=bytes)
            if audio_bytes:
                spoken_text = await asyncio.to_thread(
                    transcribe_media_bytes, groq_key, audio_bytes, file_ext or ".ogg"
                )
        except Exception:
            spoken_text = ""
        if spoken_text:
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
            try:
                thumb_desc = await asyncio.to_thread(
                    analyze_image_bytes, groq_key, thumb_bytes, caption_for_ai, "video/thumb"
                )
            except Exception:
                thumb_desc = ""
            if thumb_desc:
                parts.append(f"[المشهد المرئي في الفيديو]: {thumb_desc}")
        file_size = getattr(getattr(message, "file", None), "size", 0) or 0
        if 0 < file_size <= 20 * 1024 * 1024:
            try:
                video_bytes = await tg_client.download_media(message, file=bytes)
                if video_bytes:
                    spoken_video = await asyncio.to_thread(
                        transcribe_media_bytes, groq_key, video_bytes, file_ext or ".mp4"
                    )
                    if spoken_video:
                        parts.append(f"[الكلام المنطوق داخل الفيديو]: {spoken_video}")
            except Exception:
                pass
        return "\n".join(part for part in parts if part.strip()), "video", preview

    return f"{caption}{url_note}".strip(), "text", ""


def _worth_archiving(card: dict) -> bool:
    """None لا يرفض البطاقة. الرفض فقط إذا صرّح النموذج بـ False وخلا العنوان والحل من الفائدة."""
    return card_passes_quality_gate(card)


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


def get_last_synced_id(group_key: str = SYNC_GROUP_KEY) -> int:
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


_SAFE_SYNC_SETTING_KEYS = (
    "telegram_sync_active",
    "telegram_sync_attempt_at",
    "sync_checkpoint",
    "last_msg_id",
    "sync_offset",
)


def _wipe_sync_checkpoint(group_key: str = SYNC_GROUP_KEY) -> None:
    """يصفّر sync_state ومفاتيح حالة السحب فقط، دون المساس بالجلسة أو البطاقات."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM sync_state")
        placeholders = ",".join("?" for _ in _SAFE_SYNC_SETTING_KEYS)
        conn.execute(
            f"DELETE FROM app_settings WHERE key IN ({placeholders})",
            _SAFE_SYNC_SETTING_KEYS,
        )
        conn.execute(
            """
            INSERT INTO sync_state (group_key, last_msg_id, updated_at)
            VALUES (?, 0, CURRENT_TIMESTAMP)
            """,
            (group_key,),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        cfg = load_config()
        changed = False
        for key in ("last_msg_id", "sync_checkpoint", "sync_offset"):
            if key in cfg:
                cfg.pop(key, None)
                changed = True
        if changed:
            save_config(cfg)
    except Exception:
        pass


def reset_sync_checkpoint(group_key: str = SYNC_GROUP_KEY) -> None:
    """يمسح مؤشرات sync_state و app_settings ويصفّرها إلى 0."""
    if _SYNC_ROUND_ACTIVE:
        request_stop_sync()
    _wipe_sync_checkpoint(group_key)
    force_release_client_lock()
    if not _SYNC_ROUND_ACTIVE:
        clear_stop_sync()


def update_last_synced_id(msg_id: int, group_key: str = SYNC_GROUP_KEY) -> None:
    """يحرك نقطة الاستئناف للأمام فقط. الصفر يُستخدم عند إعادة الضبط الصريحة."""
    target = int(msg_id)
    if target != 0 and _STOP_SYNC_EVENT.is_set():
        return
    if target != 0 and target <= get_last_synced_id(group_key):
        return
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
            (group_key, target),
        )
        conn.commit()
    finally:
        conn.close()


def _advance_contiguous_checkpoint(
    active_page: list,
    resolved_ids: set[int],
    prev_cursor: int,
    group_key: str = SYNC_GROUP_KEY,
) -> int:
    """لا يرفع المؤشر إلا لآخر رسالة متصلة اكتملت معالجتها دون أي فشل تقني."""
    safe_id = int(prev_cursor)
    for msg in active_page:
        mid = int(msg.id)
        if mid in resolved_ids:
            if mid > safe_id:
                safe_id = mid
        else:
            break
    if safe_id > int(prev_cursor):
        update_last_synced_id(safe_id, group_key)
    return safe_id


def _rate_limit_wait_seconds(error: Exception) -> int:
    match = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", str(error).lower())
    if not match:
        return 18
    minutes = int(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return min(120, int(minutes * 60 + seconds) + 2)


def _is_daily_exhaustion(error: Exception) -> bool:
    low = str(error or "").lower()
    return any(
        token in low
        for token in ("استُنفدت حصة groq اليومية", "tokens per day", "requests per day", "tpd", "rpd", "per day")
    )


def _is_rate_limit(error: Exception) -> bool:
    text = str(error).lower()
    return "429" in text or "rate_limit" in text or "rate limit" in text


def _is_ai_service_error(exc: Exception) -> bool:
    """يميز أخطاء الشبكة والحصة والخدمة عن أخطاء تنسيق النص العابرة."""
    if is_key_or_quota_error(exc) or _is_blocking_failure(exc) or isinstance(exc, RuntimeError):
        return True
    low = f"{exc.__class__.__name__} {exc}".lower()
    return any(
        token in low
        for token in ("500", "502", "503", "504", "408", "تعذر", "api", "client", "connection", "timeout")
    )


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


def _sender_id(message) -> int:
    return int(getattr(message, "sender_id", None) or 0)


def _message_time(message):
    return getattr(message, "date", None)


def _grouped_id(message):
    grouped = getattr(message, "grouped_id", None)
    return grouped if grouped else None


def _build_dialogue_threads(messages: list) -> dict:
    """يجمع الرد على سؤاله وألبوم الوسائط ورسائل الكاتب خلال 3 دقائق. كل رسالة تُدرج مرة واحدة."""
    threads = {}
    message_to_root = {}
    grouped_to_root = {}
    seen_ids = set()
    previous = None
    previous_root = None
    for message in sorted(messages, key=lambda item: int(item.id)):
        message_id = int(message.id)
        if message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        reply_id = _reply_to_id(message)
        grouped = _grouped_id(message)
        root_id = message_id
        if reply_id and reply_id in message_to_root:
            root_id = message_to_root[reply_id]
        elif grouped and grouped in grouped_to_root:
            root_id = grouped_to_root[grouped]
        elif previous is not None and previous_root is not None and not reply_id:
            moment = _message_time(message)
            previous_moment = _message_time(previous)
            same_sender = _sender_id(message) and _sender_id(message) == _sender_id(previous)
            if same_sender and moment is not None and previous_moment is not None:
                if abs((moment - previous_moment).total_seconds()) <= 180:
                    root_id = previous_root
        threads.setdefault(root_id, []).append(message)
        message_to_root[message_id] = root_id
        if grouped:
            grouped_to_root[grouped] = root_id
        previous = message
        previous_root = root_id
    return threads


_EDGE_GREETINGS = (
    "السلام عليكم ورحمة الله وبركاته",
    "السلام عليكم ورحمة الله",
    "السلام عليكم",
    "سلام عليكم",
    "صباح الخير",
    "مساء الخير",
    "يعطيكم العافية",
    "يعطيك العافية",
    "يا شباب",
    "ياشباب",
    "تصبحون على خير",
)


def _strip_edge_greetings(text: str) -> str:
    """ينزع التحية والمجاملة من طرفي النص فقط حتى لا تُبتلع كلمات مثل هل واستهلاك."""
    cleaned = (text or "").strip()
    phrases = sorted(_EDGE_GREETINGS, key=len, reverse=True)
    changed = True
    while cleaned and changed:
        changed = False
        for phrase in phrases:
            if cleaned.startswith(phrase):
                cleaned = cleaned[len(phrase):].strip(" \n\t،,.:;!-ـ")
                changed = True
                break
            if cleaned.endswith(phrase):
                cleaned = cleaned[: -len(phrase)].strip(" \n\t،,.:;!-ـ")
                changed = True
                break
    return cleaned


def _remaining_is_technical(text: str) -> bool:
    if "?" in text or "؟" in text:
        return True
    lowered = text.lower()
    return any(term.lower() in lowered for term in USEFUL_TOPIC_TERMS)


_PURE_COURTESY = {
    "تسلم", "تسلمي", "مشكور", "مشكورة", "مشكورين", "شكرا", "شكراً",
    "يسلمو", "يسلموا", "الله", "يعافيك", "يعافيكم", "هلا", "أهلا", "اهلا",
    "مرحبا", "مرحباً", "حياك", "حياكم", "تمام", "اوك", "أوك", "ok",
}


def _is_pure_courtesy(text: str) -> bool:
    tokens = re.findall(r"[0-9A-Za-z\u0600-\u06FF]+", text or "")
    if not tokens:
        return True
    return all(token.lower() in _PURE_COURTESY or set(token) <= {"ه"} for token in tokens)


def _thread_is_noise(messages: list) -> bool:
    """يستبعد التحية والمجاملة الفارغة فقط. قصر النص وحده لا يسقط السلسلة."""
    texts = []
    has_link = False
    has_media = False
    for message in messages:
        text = (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()
        texts.append(text)
        if extract_urls(text):
            has_link = True
        if detect_msg_media_kind(message) != "text":
            has_media = True
    if has_link or has_media:
        return False
    combined = " ".join(part for part in texts if part)
    if "?" in combined or "؟" in combined:
        return False
    remaining = _strip_edge_greetings(combined)
    if not remaining.strip():
        return True
    if "هل" in remaining or _remaining_is_technical(remaining):
        return False
    return _is_pure_courtesy(remaining)


def _explain_sync_stop(result: dict) -> str:
    """سبب صريح عندما لا تُحفظ بطاقات أو يتوقف السحب قبل اكتمال الدفعة."""
    error = str(result.get("error") or "").strip()
    if error:
        low = error.lower()
        if any(token in low for token in ("401", "429", "quota", "rate limit", "rate_limit", "حصة", "مفتاح")):
            return f"توقف السحب لحماية الرسائل (خطأ مفتاح أو حصة Groq): {error}"
        if any(token in low for token in ("اتصال", "شبكة", "timeout", "connection", "disconnected", "network")):
            return f"توقف السحب: خطأ اتصال. {error}"
        return f"توقف السحب: {error}"
    if result.get("stopped"):
        return "توقف السحب لأن زر الإيقاف ضُغط أثناء الجولة."
    if result.get("hint"):
        return str(result["hint"])
    if int(result.get("saved") or 0) > 0:
        return ""
    if int(result.get("scanned") or 0) == 0:
        return "لا توجد رسائل جديدة. انتهت رسائل القروب عند نقطة التوقف."
    return (
        f"انتهى السحب دون حفظ بطاقات جديدة. فُحصت {result.get('scanned', 0)} رسالة، "
        f"سلاسل النقاش {result.get('threads', 0)}، واستُبعدت تحيات {result.get('skipped', 0)}."
    )


def _pull_digest(group: str, scanned: int, threads: int, skipped: int, saved: int) -> str:
    return (
        f"اسم القروب: ({group}) | فُحصت: {scanned} رسالة | "
        f"سلاسل النقاش: {threads} | استُبعدت كترحيب عابر: {skipped} | "
        f"حُفظت/حُدثت: {saved} بطاقة"
    )


def _is_blocking_failure(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(
        token in text
        for token in ("429", "rate_limit", "rate limit", "timeout", "connection", "network", "disconnected", "temporarily")
    )


def _should_analyze_message(message) -> bool:
    """يمرر كل رسالة فيها نص أو وسائط، مهما كان النص قصيراً."""
    if detect_msg_media_kind(message) != "text":
        return True
    text = (
        getattr(message, "message", None)
        or getattr(message, "text", None)
        or getattr(message, "raw_text", None)
        or ""
    )
    return bool(str(text).strip())


async def _run_with_backoff(factory, on_progress):
    """يعيد المحاولة عند ضغط الدقيقة العابر، ويتوقف فوراً إذا نفدت الحصة اليومية."""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            value = factory()
            if asyncio.iscoroutine(value):
                return await value
            return value
        except Exception as exc:
            if _is_daily_exhaustion(exc) or not _is_rate_limit(exc) or attempt + 1 >= RATE_LIMIT_RETRIES:
                raise
            pause = min(_rate_limit_wait_seconds(exc), RATE_LIMIT_PAUSE_CAP)
            _report(
                on_progress,
                f"ضغط مؤقت على Groq: تريث {pause} ثانية ثم متابعة السحب من نفس النقطة...",
                0.45,
            )
            await asyncio.sleep(pause)


async def _attach_missing_parents(client, entity, messages: list) -> list:
    """يجلب أسئلة الدفعات السابقة دفعة واحدة حتى يبقى الحل مربوطاً بسؤاله."""
    merged = list(messages)
    present = {int(message.id) for message in merged}
    for _ in range(4):
        missing_reply_ids = []
        for message in merged:
            reply_id = _reply_to_id(message)
            if reply_id and reply_id not in present and reply_id not in missing_reply_ids:
                missing_reply_ids.append(reply_id)
        if not missing_reply_ids:
            break
        fetched = await client.get_messages(entity, ids=missing_reply_ids)
        if fetched is None:
            break
        if not isinstance(fetched, (list, tuple)):
            fetched = [fetched]
        added = False
        for item in fetched:
            if item is None or not getattr(item, "id", None):
                continue
            item_id = int(item.id)
            if item_id in present:
                continue
            merged.append(item)
            present.add(item_id)
            added = True
        if not added:
            break
    return merged


async def _find_cursor_before_date(client, target, start_date: datetime) -> int:
    """يجد آخر رسالة أقدم من 1/1/2026. إذا لم تكن النتيجة أقدم من التاريخ نبدأ من 0."""
    try:
        async for message in client.iter_messages(target, offset_date=start_date, limit=1):
            msg_dt = getattr(message, "date", None)
            if msg_dt is None:
                return 0
            if msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=timezone.utc)
            if msg_dt < start_date:
                return int(message.id)
            return 0
    except Exception:
        pass
    return 0


async def _resolve_author_name(message, sender_cache: dict[int, str]) -> str:
    """يقرأ اسم الكاتب من الكائن المحمّل أو الذاكرة المؤقتة لتقليل طلبات الشبكة."""
    sid = _sender_id(message)
    if sid and sid in sender_cache:
        return sender_cache[sid]
    sender = getattr(message, "sender", None)
    if sender is None:
        try:
            sender = await message.get_sender()
        except Exception:
            sender = None
    if sender is not None:
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        name = f"{first} {last}".strip() or getattr(sender, "title", None) or "عضو"
    else:
        name = "عضو"
    if sid:
        sender_cache[sid] = name
    return name


def _build_thread_url(public_username: str, public_id: str, root_id: int) -> str:
    if public_username:
        return f"https://t.me/{public_username}/{root_id}"
    return f"https://t.me/c/{public_id}/{root_id}"


async def _forward_page(client, target, after_id: int, page_size: int) -> list:
    """يجلب صفحة تصاعدية من الرسالة التالية لـ after_id. لا يُستخدم offset_date مع reverse."""
    page = []
    async for message in client.iter_messages(
        target,
        min_id=int(after_id),
        reverse=True,
        limit=page_size,
    ):
        if int(message.id) <= int(after_id):
            continue
        page.append(message)
    page.sort(key=lambda item: item.id)
    return page


def _stored_card_count() -> int:
    conn = get_db_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0])
    finally:
        conn.close()


async def _open_client(cfg: dict, logs: list):
    """يجرب جلسة Secrets أولاً ثم البدائل المحلية، ويصلح القاعدة تلقائياً عند الاتصال."""
    api_id, api_hash = _api_credentials(cfg)
    sessions = _candidate_sessions(cfg)
    for session_text in sessions:
        try:
            client = TelegramClient(StringSession(session_text), api_id, api_hash, timeout=30, receive_updates=False)
        except Exception:
            set_app_setting("invalid_tg_session", session_text)
            continue
        try:
            if await _connect_saved_client(client, logs, "تم الاتصال بجلسة تيليجرام النصية بنجاح."):
                set_app_setting("telegram_string_session", session_text)
                set_app_setting("telegram_session_status", "ok")
                set_app_setting("invalid_tg_session", "")
                return client, None
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            if "authorization key" in str(exc).lower() or "AuthKeyDuplicated" in str(exc):
                set_app_setting("invalid_tg_session", session_text)
                continue
            raise
        set_app_setting("invalid_tg_session", session_text)
    if sessions:
        mark_telegram_session_invalid()
        return None, "جلسة تيليجرام الحالية أُلغيت من تيليجرام. فعّل جلسة جديدة مرة واحدة وضعها في TELEGRAM_SESSION داخل Secrets."
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


async def sync_and_analyze(
    limit: int = 50,
    on_progress=None,
    api_key: str = "",
    skip_media: bool = False,
    force_from_start: bool = False,
    use_batch_ai: bool = False,
) -> dict:
    logs = []
    result = {
        "ok": False, "saved": 0, "scanned": 0, "threads": 0, "skipped": 0,
        "group": "", "error": None, "logs": logs,
    }
    init_db()
    cfg = load_config()
    groq_key = (collect_groq_keys(api_key) or [""])[0]
    if not groq_key:
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

        group_name = target.name or str(target.id)
        result["group"] = group_name
        chat_id = str(target.id)
        public_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")
        public_username = (getattr(target.entity, "username", None) or "").strip()
        group_key = SYNC_GROUP_KEY
        requested = max(1, min(SESSION_PULL_CAP, int(limit or SESSION_PULL_CAP)))

        def _span(done_units: float) -> float:
            return min(0.98, 0.08 + 0.90 * (done_units / requested))

        ignore_date_filter = bool(force_from_start)
        if ignore_date_filter:
            _wipe_sync_checkpoint(group_key)
            cursor_id = 0
            start_note = (
                f"يبدأ السحب الشامل من الرسالة رقم 1 نحو الأحدث (شامل كل التواريخ). "
                f"المطلوب {requested} رسالة على دفعات من {SYNC_CHAIN_BATCH}."
            )
        else:
            last_id = get_last_synced_id(group_key)
            if last_id <= 0:
                _report(on_progress, "جاري تحديد موقع أول رسالة بتاريخ 1/1/2026...", 0.08)
                cursor_id = await _find_cursor_before_date(client, target, START_SYNC_DATE)
                start_note = (
                    f"يبدأ السحب من تاريخ 1/1/2026 (بعد الرسالة #{cursor_id}) نحو الأحدث. "
                    f"المطلوب {requested} رسالة على دفعات من {SYNC_CHAIN_BATCH}."
                )
            else:
                cursor_id = last_id
                start_note = (
                    f"يكمل السحب تصاعدياً من الرسالة رقم {last_id + 1} نحو الأحدث. "
                    f"المطلوب {requested} رسالة على دفعات من {SYNC_CHAIN_BATCH}."
                )
        _report(on_progress, start_note, 0.09)
        _report(on_progress, f"نقطة البداية الفعلية بعد الفحص: من الرسالة رقم {cursor_id}", 0.095)
        _report(on_progress, "المؤشر محمي: لا يتقدم إلا للرسائل التي اكتمل تحليلها بنجاح.", 0.10)
        _note(logs, f"متابعة أرشفة {group_name} بعد الرسالة {cursor_id}. المطلوب {requested}.")

        sender_cache: dict[int, str] = {}
        pulled = 0
        batch_no = 0
        empty_date_pages = 0
        consecutive_ai_errors = 0
        while pulled < requested:
            if _STOP_SYNC_EVENT.is_set():
                result["stopped"] = True
                result["ok"] = True
                _report(on_progress, "تم إيقاف السحب. التقدم محفوظ عند آخر نقاش اكتملت معالجته.", _span(pulled))
                break
            batch_no += 1
            this_limit = min(SYNC_CHAIN_BATCH, requested - pulled)
            batch_base = pulled
            prev_cursor = cursor_id
            _report(
                on_progress,
                f"الدفعة {batch_no}: سحب {this_limit} رسالة من الأقدم إلى الأحدث. المفحوص {pulled} من {requested}.",
                _span(batch_base),
            )
            try:
                page = await _forward_page(client, target, cursor_id, this_limit)
            except FloodWaitError as exc:
                pause = min(int(getattr(exc, "seconds", BATCH_PAUSE_SECONDS)) + 1, RATE_LIMIT_PAUSE_CAP)
                _report(on_progress, f"تيليجرام طلب التريث {pause} ثانية. السحب يتابع من الرسالة {cursor_id}.", _span(batch_base))
                await asyncio.sleep(pause)
                continue
            if not page or page[-1].id <= cursor_id:
                saved_checkpoint = get_last_synced_id(group_key)
                if pulled == 0 and not ignore_date_filter and saved_checkpoint > 0:
                    stall = (
                        f"المؤشر متوقف عند آخر رسالة رقم {saved_checkpoint} — "
                        "اضغط زر (إعادة الضبط للبدء من 1/1/2026) أو (سحب القروب كاملاً من البداية)."
                    )
                elif pulled == 0:
                    stall = "لا توجد رسائل جديدة بعد نقطة التوقف الحالية."
                else:
                    stall = (
                        f"انتهت رسائل القروب. لا توجد رسائل أحدث. "
                        f"فُحصت {result['scanned']} وحُفظت {result['saved']}."
                    )
                result["hint"] = stall
                _report(on_progress, stall, _span(pulled))
                break
            active_page = []
            raw_messages = []
            resolved_page_ids: set[int] = set()
            fetched = 0
            for message in page:
                active_page.append(message)
                if not ignore_date_filter and not _message_is_after_start(message):
                    resolved_page_ids.add(int(message.id))
                    continue
                fetched += 1
                pulled += 1
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
                if pulled >= requested:
                    break
            next_cursor_id = int(active_page[-1].id)
            if not raw_messages:
                cursor_id = _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                empty_date_pages += 1
                if empty_date_pages >= 50:
                    result["hint"] = "تجاوزنا صفحات ما قبل 1/1/2026 دون العثور على رسائل أحدث."
                    break
                continue
            empty_date_pages = 0
            referenced_ids = {reply_id for reply_id in (_reply_to_id(message) for message in raw_messages) if reply_id}
            batch = []
            for message in raw_messages:
                if _should_analyze_message(message) or _reply_to_id(message) or message.id in referenced_ids:
                    batch.append(message)
                else:
                    resolved_page_ids.add(int(message.id))
            try:
                batch = await _attach_missing_parents(client, target, batch)
            except Exception as exc:
                if _is_blocking_failure(exc):
                    result["error"] = "تعذر جلب الرسائل الأصلية بسبب انقطاع الشبكة. المؤشر لم يتقدم."
                    _note(logs, result["error"], "error")
                    break
                _note(logs, f"تعذر جلب بعض الرسائل الأصلية: {exc}", "error")
            threads = _build_dialogue_threads(batch)
            result["threads"] += len(threads)
            thread_total = max(len(threads), 1)
            hold_checkpoint = False
            stopped_page = False
            _report(
                on_progress,
                _pull_digest(group_name, result["scanned"], result["threads"], result["skipped"], result["saved"]),
                _span(batch_base),
            )
            ordered_threads = sorted(
                threads.items(),
                key=lambda item: min((getattr(message, "id", 0) or 0) for message in item[1]),
            )
            prepared_threads = []
            for thread_index, (root_id, thread_messages) in enumerate(ordered_threads, start=1):
                if _STOP_SYNC_EVENT.is_set():
                    stopped_page = True
                    break
                thread_msg_ids = {int(getattr(m, "id", 0) or 0) for m in thread_messages}
                if _thread_is_noise(thread_messages):
                    result["skipped"] += 1
                    resolved_page_ids.update(thread_msg_ids)
                    _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                    _report(
                        on_progress,
                        _pull_digest(group_name, result["scanned"], result["threads"], result["skipped"], result["saved"]),
                        _span(batch_base),
                    )
                    continue
                fraction = _span(batch_base + fetched * (0.45 + 0.55 * thread_index / thread_total))
                blocks = []
                dominant_media = "text"
                best_thumb = ""
                author_name = "عضو"
                for index, message in enumerate(thread_messages):
                    role = "الرسالة الأساسية (طرح المشكلة أو الموضوع)" if index == 0 else f"الرد رقم {index} (الإجابة أو الحل)"
                    if index == 0:
                        author_name = await _resolve_author_name(message, sender_cache)
                    try:
                        parsed_text, media_kind, media_b64 = await _run_with_backoff(
                            lambda message=message: _extract_message_content(client, groq_key, message, skip_media),
                            on_progress,
                        )
                    except Exception as exc:
                        if _is_blocking_failure(exc) or is_key_or_quota_error(exc):
                            hold_checkpoint = True
                            result["error"] = f"تعذر قراءة الرسائل أو الوسائط: {describe_groq_error(exc)}"
                            _note(logs, result["error"], "error")
                            break
                        _note(logs, f"تخطي مرفق الرسالة {message.id}: {exc}", "error")
                        _report(on_progress, f"الرسالة {message.id}: تعذر قراءة وسائطها.", fraction)
                        continue
                    if (parsed_text or "").strip():
                        blocks.append(f"--- {role} (نوعها: {media_kind}) ---\n{parsed_text}")
                    if {"text": 0, "image": 1, "audio": 2, "video": 3}[media_kind] > {"text": 0, "image": 1, "audio": 2, "video": 3}[dominant_media]:
                        dominant_media = media_kind
                    if media_b64 and not best_thumb:
                        best_thumb = media_b64
                if hold_checkpoint:
                    break
                thread_text = "\n\n".join(blocks).strip()
                if not thread_text:
                    resolved_page_ids.update(thread_msg_ids)
                    _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                    continue
                thread_entry = {
                    "root_id": root_id,
                    "thread_msg_ids": thread_msg_ids,
                    "thread_text": thread_text,
                    "author_name": author_name,
                    "dominant_media": dominant_media,
                    "best_thumb": best_thumb,
                    "fraction": fraction,
                }
                if use_batch_ai:
                    prepared_threads.append(thread_entry)
                else:
                    _report(
                        on_progress,
                        f"الدفعة {batch_no}: تحليل النقاش رقم {root_id} عبر Groq | بطاقات {result['saved']}",
                        fraction,
                    )
                    try:
                        card = await _run_with_backoff(
                            lambda thread_text=thread_text, fraction=fraction: asyncio.to_thread(
                                analyze_thread_with_ai,
                                thread_text,
                                groq_key,
                                None,
                                None,
                                lambda note, fraction=fraction: _report(on_progress, note, fraction),
                                group_name,
                            ),
                            on_progress,
                        )
                        consecutive_ai_errors = 0
                    except Exception as exc:
                        consecutive_ai_errors += 1
                        clean_err = describe_groq_error(exc)
                        notice = f"خطأ Groq في النقاش {root_id}: {clean_err}"
                        _note(logs, notice, "error")
                        _report(on_progress, notice, fraction)
                        if _is_ai_service_error(exc) or consecutive_ai_errors >= 3:
                            hold_checkpoint = True
                            result["error"] = notice
                            break
                        resolved_page_ids.update(thread_msg_ids)
                        _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                        continue
                    await asyncio.sleep(AI_REQUEST_PACING_SECONDS)
                    reason = card_rejection_reason(card)
                    thread_url = _build_thread_url(public_username, public_id, root_id)
                    if reason or save_card(
                        card,
                        root_id,
                        thread_url,
                        author_name,
                        thread_text,
                        dominant_media,
                        best_thumb,
                        group_name=group_name,
                    ) is not True:
                        rejection = reason or card_rejection_reason(card) or "لم تُحفظ البطاقة"
                        notice = f"استبعاد النقاش {root_id}: {rejection}"
                        _note(logs, notice, "info")
                        _report(on_progress, notice, fraction)
                        resolved_page_ids.update(thread_msg_ids)
                        _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                        continue
                    result["saved"] += 1
                    resolved_page_ids.update(thread_msg_ids)
                    _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                    _note(logs, f"تم حفظ بطاقة: [{card.get('category')}] {card.get('title')}")
                    _report(
                        on_progress,
                        _pull_digest(group_name, result["scanned"], result["threads"], result["skipped"], result["saved"]),
                        fraction,
                    )
            if use_batch_ai and prepared_threads and not hold_checkpoint and not stopped_page:
                for start_idx in range(0, len(prepared_threads), AI_BATCH_SIZE):
                    if _STOP_SYNC_EVENT.is_set():
                        stopped_page = True
                        break
                    chunk = prepared_threads[start_idx:start_idx + AI_BATCH_SIZE]
                    payload = [{"thread_id": item["root_id"], "text": item["thread_text"]} for item in chunk]
                    chunk_fraction = chunk[-1]["fraction"]
                    _report(
                        on_progress,
                        f"الدفعة {batch_no}: تحليل حزمة من {len(chunk)} نقاشات عبر Groq | بطاقات {result['saved']}",
                        chunk_fraction,
                    )
                    try:
                        batch_cards = await _run_with_backoff(
                            lambda payload=payload, chunk_fraction=chunk_fraction: asyncio.to_thread(
                                analyze_threads_batch_with_ai,
                                payload,
                                groq_key,
                                group_name,
                                lambda note, chunk_fraction=chunk_fraction: _report(on_progress, note, chunk_fraction),
                            ),
                            on_progress,
                        )
                    except Exception as exc:
                        hold_checkpoint = True
                        result["error"] = f"خطأ Groq أثناء التحليل المجمّع: {describe_groq_error(exc)}"
                        _note(logs, result["error"], "error")
                        break
                    cards_by_id = {
                        int(c["thread_id"]): c
                        for c in batch_cards
                        if isinstance(c, dict) and c.get("thread_id") is not None
                    }
                    for item in chunk:
                        root_id = item["root_id"]
                        card = cards_by_id.get(int(root_id)) or {}
                        if card.get("_error"):
                            consecutive_ai_errors += 1
                            raw_err = str(card.get("_error") or "")
                            notice = f"خطأ Groq في النقاش {root_id}: {card.get('_error_type')}: {raw_err[:120]}"
                            _note(logs, notice, "error")
                            _report(on_progress, notice, chunk_fraction)
                            if card.get("_key_failure") or card.get("_error_type") == "RuntimeError" or consecutive_ai_errors >= 2:
                                hold_checkpoint = True
                                result["error"] = notice
                                break
                            continue
                        consecutive_ai_errors = 0
                        reason = card_rejection_reason(card)
                        thread_url = _build_thread_url(public_username, public_id, root_id)
                        if reason or save_card(
                            card,
                            root_id,
                            thread_url,
                            item["author_name"],
                            item["thread_text"],
                            item["dominant_media"],
                            item["best_thumb"],
                            group_name=group_name,
                        ) is not True:
                            resolved_page_ids.update(item["thread_msg_ids"])
                            _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                            continue
                        result["saved"] += 1
                        resolved_page_ids.update(item["thread_msg_ids"])
                        _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                        _note(logs, f"تم حفظ بطاقة: [{card.get('category')}] {card.get('title')}")
                    _report(
                        on_progress,
                        _pull_digest(group_name, result["scanned"], result["threads"], result["skipped"], result["saved"]),
                        chunk_fraction,
                    )
                    if hold_checkpoint:
                        break
                    await asyncio.sleep(AI_REQUEST_PACING_SECONDS * 2)
            if stopped_page:
                _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                result["stopped"] = True
                result["ok"] = True
                _report(on_progress, "تم إيقاف السحب. التقدم محفوظ عند آخر نقاش اكتملت معالجته.", _span(batch_base))
                break
            if hold_checkpoint:
                safe_saved_at = _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
                _report(
                    on_progress,
                    f"توقف آمن عند الرسالة #{safe_saved_at} لحماية الرسائل المتبقية من الضياع.",
                    _span(batch_base),
                )
                break
            cursor_id = _advance_contiguous_checkpoint(active_page, resolved_page_ids, prev_cursor, group_key)
            if cursor_id < next_cursor_id and resolved_page_ids.issuperset(int(message.id) for message in active_page):
                cursor_id = next_cursor_id
            _report(
                on_progress,
                _pull_digest(group_name, result["scanned"], result["threads"], result["skipped"], result["saved"]),
                _span(pulled),
            )
            if pulled >= requested:
                break
            await asyncio.sleep(BATCH_PAUSE_SECONDS)

        if not result.get("error"):
            result["ok"] = True
        reason = _explain_sync_stop(result)
        if reason:
            result["hint"] = result.get("hint") or reason
            _note(logs, reason, "error" if result.get("error") or int(result.get("saved") or 0) == 0 else "info")
            _report(on_progress, reason, _span(pulled))
        elif result.get("stopped"):
            _note(logs, f"توقف السحب بطلب المشرف. الرسائل المفحوصة: {result['scanned']}، البطاقات المحفوظة: {result['saved']}")
        else:
            _note(logs, f"اكتمل السحب. الرسائل المفحوصة: {result['scanned']}، البطاقات المحفوظة: {result['saved']}")
        return result
    except Exception as exc:
        result["error"] = _telegram_error_message(exc)
        reason = _explain_sync_stop(result)
        _note(logs, reason, "error")
        _report(on_progress, reason, 1.0)
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


def _run_on_fresh_loop(factory):
    """يفتح event loop مستقلاً حتى لا يشارك عميل Telethon حلقة Streamlit."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


def _run_login(factory):
    acquired = False
    try:
        acquired = _CLIENT_LOCK.acquire(timeout=20)
        if not acquired:
            return {"ok": False, "error": "توجد محاولة اتصال جارية. انتظر ثواني ثم أعد المحاولة."}
        _set_sync_active(True)
        return _run_on_fresh_loop(factory)
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


def run_sync(
    limit: int = 50,
    on_progress=None,
    api_key: str = "",
    skip_media: bool = False,
    force_from_start: bool = False,
    use_batch_ai: bool = False,
) -> dict:
    """نقطة التشغيل التي يستدعيها زر Streamlit دون فتح الطرفية."""
    global _SYNC_ROUND_ACTIVE
    init_db()
    _prepare_sync_round()
    chosen_key = str(api_key or os.environ.get("GROQ_API_KEY") or "").strip()
    media_skipped = bool(skip_media)
    start_over = bool(force_from_start)
    batched = bool(use_batch_ai)
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
                "logs": ["توجد عملية سحب جارية حالياً. انتظر حتى تُغلق جلسة تيليجرام."],
            }
        _SYNC_ROUND_ACTIVE = True
        if start_over:
            clear_stop_sync()
            _wipe_sync_checkpoint(SYNC_GROUP_KEY)
        _set_sync_active(True)
        set_app_setting("telegram_sync_attempt_at", str(time.time()))
        return _run_on_fresh_loop(
            lambda: sync_and_analyze(
                limit=limit,
                on_progress=on_progress,
                api_key=chosen_key,
                skip_media=media_skipped,
                force_from_start=start_over,
                use_batch_ai=batched,
            )
        )
    finally:
        if acquired:
            _SYNC_ROUND_ACTIVE = False
            try:
                _CLIENT_LOCK.release()
            except RuntimeError:
                pass
            _set_sync_active(False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mode", type=str, default="history")
    parser.add_argument("--batch-ai", action="store_true")
    args, _ = parser.parse_known_args()
    summary = run_sync(limit=args.limit, use_batch_ai=args.batch_ai)
    if not summary["ok"]:
        raise SystemExit(summary["error"] or "فشل السحب")
