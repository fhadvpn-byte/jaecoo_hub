import base64
import hmac
import html
import os
import secrets
from pathlib import Path
import threading
import time
from datetime import timedelta

import pandas as pd
import streamlit as st

from database_and_ai import (
    analyze_image_bytes,
    analyze_thread_with_ai,
    compress_image_to_b64,
    find_matching_solution_in_db,
    generate_daily_pulse_summary,
    get_db_connection,
    init_db,
    load_config,
    save_card,
    save_config,
    transcribe_media_bytes,
)
from telegram_scraper import PERSONAL_SESSION_FILE, run_sync

AUTO_SYNC_LIMIT = 30
JAECOO_MODELS = [
    "Jaecoo J8",
    "Jaecoo J8 SHS",
    "Jaecoo J7",
    "Jaecoo J5",
    "جميع الطرازات / عام",
]
MEDIA_BADGES = {
    "text": {"icon": "📝", "label": "رسالة نصية", "color": "#38bdf8"},
    "image": {"icon": "🖼️", "label": "تحليل صورة", "color": "#a855f7"},
    "audio": {"icon": "🎙️", "label": "مقطع صوتي", "color": "#10b981"},
    "video": {"icon": "🎥", "label": "تحليل فيديو", "color": "#f59e0b"},
}
_SYNC_GUARD = threading.Lock()
_SYNC_STATE = {
    "running": False,
    "done_at": 0.0,
    "result": None,
    "lines": [],
    "fraction": 0.0,
    "message": "",
}


def clean_database() -> None:
    """يزيل nan والبطاقات الإنجليزية العشوائية عند فتح التطبيق."""
    conn = get_db_connection()
    try:
        conn.execute(
            """
            UPDATE knowledge_cards
            SET part_numbers_or_specs = 'غير محدد'
            WHERE part_numbers_or_specs IS NULL
               OR LOWER(TRIM(part_numbers_or_specs)) IN ('nan', 'none', '', 'null')
            """
        )
        conn.execute(
            """
            UPDATE knowledge_cards
            SET title = 'الاعتمادية وتكلفة قطع الغيار والعزل لسيارة Jaecoo J5',
                problem_or_topic = 'استفسار حول مستوى الاعتمادية، أسعار قطع الغيار، وجودة العزل في طراز Jaecoo J5 مع ملاحظة حول توفر الوكيل والصيانة الذاتية.',
                solution_or_summary = 'يُنصح بمراجعة جدول الصيانة الدورية ومقارنة أسعار القطع الاستهلاكية لدى الوكيل المعتمد ومتاجر القطع البديلة.',
                part_numbers_or_specs = 'غير محدد'
            WHERE title LIKE '%Reliability and parts cost for Jaecoo J5%'
            """
        )
        conn.execute(
            """
            DELETE FROM knowledge_cards
            WHERE title LIKE '%جزء جميلة%'
               OR problem_or_topic LIKE '%User asks about%'
            """
        )
        conn.commit()
    finally:
        conn.close()


def delete_knowledge_card(card_id: int) -> None:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM knowledge_cards WHERE id = ?", (int(card_id),))
        conn.commit()
    finally:
        conn.close()


def _image_preview(image_bytes: bytes | None) -> str:
    if not image_bytes:
        return ""
    try:
        return compress_image_to_b64(image_bytes)
    except Exception:
        return ""


def _prepare_lab_source(lab_text: str, lab_file, api_key: str) -> tuple[str, str, str]:
    """يحوّل مرفق المختبر إلى نص عربي مع نوع الوسيط ونسخة الصورة المضغوطة."""
    caption = (lab_text or "").strip()
    if lab_file is None:
        return caption, "text", ""
    media_bytes = lab_file.read()
    mime_type = lab_file.type or ""
    extension = os.path.splitext(lab_file.name or "")[1].lower() or ".bin"
    if mime_type.startswith("image"):
        vision_text = analyze_image_bytes(api_key, media_bytes, caption, mime_type or "image/jpeg")
        preview = _image_preview(media_bytes)
        return f"محتوى الصورة: {vision_text}\nتعليق العضو: {caption}".strip(), "image", preview
    if mime_type.startswith("audio"):
        spoken_text = transcribe_media_bytes(api_key, media_bytes, extension)
        return f"تفريغ المقطع الصوتي: {spoken_text}\nتعليق مرفق: {caption}".strip(), "audio", ""
    if mime_type.startswith("video"):
        spoken_text = transcribe_media_bytes(api_key, media_bytes, extension or ".mp4")
        return f"الكلام المسموع في الفيديو: {spoken_text}\nتعليق الفيديو: {caption}".strip(), "video", ""
    return caption or "حلل المرفق", "text", ""


def _card_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _record_sync_progress(message: str, fraction: float) -> None:
    with _SYNC_GUARD:
        _SYNC_STATE["lines"].append(message)
        _SYNC_STATE["fraction"] = fraction
        _SYNC_STATE["message"] = message


def _auto_sync_worker():
    try:
        result = run_sync(AUTO_SYNC_LIMIT, on_progress=_record_sync_progress)
    except Exception as exc:
        result = {"ok": False, "saved": 0, "error": str(exc), "logs": []}
    with _SYNC_GUARD:
        _SYNC_STATE["result"] = result
        _SYNC_STATE["done_at"] = time.time()
        _SYNC_STATE["running"] = False


def start_auto_sync(config: dict):
    has_api = bool(str(config.get("tg_api_id") or "").strip() and str(config.get("tg_api_hash") or "").strip())
    has_login = bool(str(config.get("tg_session") or "").strip() or os.path.exists(PERSONAL_SESSION_FILE))
    if not (config.get("groq_api_key") or config.get("gemini_api_key")) or not (has_api or has_login):
        return
    with _SYNC_GUARD:
        if _SYNC_STATE["running"]:
            return
        if time.time() - _SYNC_STATE["done_at"] < 180:
            return
        _SYNC_STATE["running"] = True
        _SYNC_STATE["result"] = None
        _SYNC_STATE["lines"] = ["جاري الاتصال بجلسة الحساب..."]
        _SYNC_STATE["fraction"] = 0.08
        _SYNC_STATE["message"] = "جاري الاتصال بجلسة الحساب..."
    threading.Thread(target=_auto_sync_worker, daemon=True).start()


@st.fragment(run_every=timedelta(seconds=6))
def auto_sync_status():
    with _SYNC_GUARD:
        running = _SYNC_STATE["running"]
        result = _SYNC_STATE["result"]
        done_at = _SYNC_STATE["done_at"]
    if running:
        st.info("جاري سحب رسائل القروب وتحليلها بمفتاح Groq. البطاقات الحالية ظاهرة أثناء التحديث.")
        return
    seen = st.session_state.get("seen_sync_done_at", 0)
    if not result or done_at == seen:
        return
    st.session_state.seen_sync_done_at = done_at
    st.session_state.auto_sync_result = result
    if result.get("saved"):
        st.rerun()

# ==========================================
# 1. إعدادات الصفحة والتصميم (هوية حسم الذكية)
# ==========================================
PAGE_TITLE = "منصة جايكو الذكية للأعطال والحلول"

st.set_page_config(
    page_title=PAGE_TITLE,
    page_icon="💠",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _publish_share_title(title: str) -> None:
    """يثبت عنوان HTML الذي يقرأه تيليجرام قبل تشغيل واجهة Streamlit."""
    try:
        index = Path(st.__file__).resolve().parent / "static" / "index.html"
        html_text = index.read_text(encoding="utf-8")
        replacement = (
            f"<title>{title}</title>\n"
            f'    <meta property="og:title" content="{title}" />\n'
            f'    <meta name="twitter:title" content="{title}" />'
        )
        if f"<title>{title}</title>" in html_text and 'property="og:title"' in html_text:
            return
        updated = html_text.replace("<title>Streamlit</title>", replacement, 1)
        if updated == html_text:
            return
        index.write_text(updated, encoding="utf-8")
    except Exception:
        return


_publish_share_title(PAGE_TITLE)

_SETTING_SOURCES = {
    "groq_api_key": ("groq_api_key", "GROQ_API_KEY", "gemini_api_key"),
    "tg_api_id": ("tg_api_id", "telegram_api_id", "TG_API_ID"),
    "tg_api_hash": ("tg_api_hash", "telegram_api_hash", "TG_API_HASH"),
    "tg_group": ("tg_group", "TG_GROUP"),
    "bot_token": ("bot_token", "telegram_bot_token", "BOT_TOKEN"),
    "tg_session": ("tg_session", "telegram_session", "TG_SESSION", "TELEGRAM_SESSION"),
}


def _read_secret(names: tuple[str, ...]) -> str:
    try:
        secrets = st.secrets
    except Exception:
        return ""
    bags = [secrets]
    for section in ("telegram", "groq"):
        try:
            if section in secrets:
                bags.append(secrets[section])
        except Exception:
            continue
    for bag in bags:
        for name in names:
            try:
                value = bag.get(name, "")
            except Exception:
                value = ""
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _read_saved(cfg: dict, names: tuple[str, ...]) -> str:
    for name in names:
        text = str(cfg.get(name) or "").strip()
        if text:
            return text
    return ""


def read_cloud_string_session() -> str:
    """يقرأ String Session من st.secrets['TELEGRAM_SESSION'] لسحب السحابة."""
    try:
        return str(st.secrets["TELEGRAM_SESSION"] or "").strip()
    except Exception:
        return ""


def apply_cloud_string_session(cfg: dict) -> dict:
    secret = read_cloud_string_session() or _read_secret(("TELEGRAM_SESSION", "tg_session", "telegram_session"))
    if secret:
        cfg["tg_session"] = secret
        os.environ["TELEGRAM_SESSION"] = secret
    else:
        saved = str(cfg.get("tg_session") or "").strip()
        if saved:
            os.environ["TELEGRAM_SESSION"] = saved
    return cfg


def hydrate_config(cfg: dict) -> dict:
    """يعيد قراءة config.json و st.secrets في كل تحميل ويحفظ المفاتيح الناقصة."""
    changed = False
    for key, names in _SETTING_SOURCES.items():
        current = str(cfg.get(key) or "").strip()
        saved = _read_saved(cfg, names)
        secret = _read_secret(names)
        chosen = secret or saved or current
        if chosen and chosen != current:
            cfg[key] = chosen
            changed = True
    if "auto_reply_enabled" not in cfg:
        flag = _read_secret(("auto_reply_enabled", "AUTO_REPLY_ENABLED"))
        cfg["auto_reply_enabled"] = flag.lower() in {"1", "true", "yes", "on"} if flag else False
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def _settings_fingerprint(cfg: dict) -> tuple:
    return tuple(str(cfg.get(key) or "") for key in (*_SETTING_SOURCES, "auto_reply_enabled"))


def _load_settings_into_session(cfg: dict) -> None:
    fingerprint = _settings_fingerprint(cfg)
    if st.session_state.get("_settings_fp") == fingerprint:
        return
    st.session_state["settings_groq"] = str(cfg.get("groq_api_key") or "")
    st.session_state["settings_tg_id"] = str(cfg.get("tg_api_id") or "")
    st.session_state["settings_tg_hash"] = str(cfg.get("tg_api_hash") or "")
    st.session_state["settings_tg_group"] = str(cfg.get("tg_group") or "")
    st.session_state["settings_bot_token"] = str(cfg.get("bot_token") or "")
    st.session_state["settings_tg_session"] = str(cfg.get("tg_session") or "")
    st.session_state["settings_auto_reply"] = bool(cfg.get("auto_reply_enabled", False))
    st.session_state["_settings_fp"] = fingerprint


def ensure_admin_token(cfg: dict) -> dict:
    secret = _read_secret(("admin_token", "ADMIN_TOKEN"))
    current = str(cfg.get("admin_token") or "").strip()
    if secret and secret != current:
        cfg["admin_token"] = secret
        save_config(cfg)
    elif not current:
        cfg["admin_token"] = secrets.token_urlsafe(24)
        save_config(cfg)
    return cfg


def viewer_is_admin(cfg: dict) -> bool:
    if st.session_state.get("is_admin"):
        return True
    expected = str(cfg.get("admin_token") or "")
    provided = st.query_params.get("admin", "")
    if isinstance(provided, (list, tuple)):
        provided = provided[0] if provided else ""
    provided = str(provided or "")
    if not expected or not provided or len(expected) != len(provided):
        return False
    if not hmac.compare_digest(provided, expected):
        return False
    st.session_state.is_admin = True
    try:
        del st.query_params["admin"]
    except Exception:
        pass
    return True


init_db()
clean_database()
cfg = apply_cloud_string_session(ensure_admin_token(hydrate_config(load_config())))
is_admin = viewer_is_admin(cfg)
start_auto_sync(cfg)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap');

    .stApp {
        direction: rtl;
        text-align: right;
        font-family: 'Tajawal', sans-serif !important;
        background:
            radial-gradient(circle at 12% 18%, rgba(0, 210, 255, 0.14) 0%, transparent 35%),
            radial-gradient(circle at 88% 82%, rgba(14, 165, 233, 0.12) 0%, transparent 40%),
            radial-gradient(circle at 50% 5%, rgba(56, 189, 248, 0.10) 0%, transparent 45%),
            linear-gradient(160deg, #040914 0%, #091526 50%, #050c18 100%) !important;
        color: #e2e8f0;
    }
    .stApp :is(h1, h2, h3, h4, h5, h6, p, label, button, input, textarea) {
        font-family: 'Tajawal', sans-serif;
    }
    [data-testid="stIconMaterial"],
    .material-symbols-rounded {
        font-family: "Material Symbols Rounded" !important;
    }
    section[data-testid="stSidebar"] {
        direction: rtl;
        text-align: right;
        background: rgba(7, 16, 30, 0.85) !important;
        backdrop-filter: blur(20px) !important;
        border-left: 1px solid rgba(56, 189, 248, 0.3) !important;
        box-shadow: -5px 0 25px rgba(0, 195, 255, 0.08) !important;
    }
    .hero-box {
        direction: rtl !important;
        text-align: right !important;
        background: rgba(13, 27, 48, 0.65);
        backdrop-filter: blur(16px);
        border: 1px solid rgba(56, 189, 248, 0.4);
        border-radius: 16px;
        padding: 24px 28px;
        margin-bottom: 22px;
        position: relative;
        overflow: hidden;
        box-shadow:
            0 10px 35px rgba(0, 0, 0, 0.5),
            0 0 25px rgba(14, 165, 233, 0.2),
            inset 0 0 20px rgba(56, 189, 248, 0.08);
    }
    .hero-box::before {
        content: "";
        position: absolute;
        top: 0;
        right: 0;
        left: 0;
        height: 3px;
        background: linear-gradient(90deg, transparent, #00f2fe, #38bdf8, transparent);
        box-shadow: 0 0 15px #00f2fe;
    }
    .hero-box h1,
    .hero-box p {
        direction: rtl !important;
        text-align: right !important;
        margin-right: 0;
        margin-left: 0;
    }
    .hero-box h1 {
        margin: 8px 0 0;
        font-size: 1.9rem;
        font-weight: 800;
        color: #ffffff;
        text-shadow: 0 0 18px rgba(56, 189, 248, 0.6);
    }
    .hero-box p {
        color: #9ec5e8;
        margin-top: 8px;
    }
    .hub-badge {
        display: inline-block;
        direction: ltr;
        background: rgba(14, 165, 233, 0.12);
        color: #38bdf8;
        border: 1px solid rgba(56, 189, 248, 0.35);
        padding: 3px 12px;
        border-radius: 20px;
        font-size: 0.88rem;
        letter-spacing: 1.5px;
        box-shadow: 0 0 12px rgba(56, 189, 248, 0.2);
    }
    .metric-card {
        background: rgba(13, 27, 48, 0.65);
        backdrop-filter: blur(16px);
        border: 1px solid rgba(56, 189, 248, 0.28);
        border-radius: 14px;
        padding: 16px 20px;
        text-align: center;
        box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.37),
            inset 0 0 15px rgba(56, 189, 248, 0.06);
        transition: all 0.3s ease;
    }
    .metric-card:hover {
        border-color: rgba(0, 242, 254, 0.7);
        box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.5),
            0 0 20px rgba(56, 189, 248, 0.3);
        transform: translateY(-2px);
    }
    .metric-val {
        font-size: 2.2rem;
        font-weight: 800;
        color: #38bdf8;
        text-shadow: 0 0 14px rgba(56, 189, 248, 0.5);
    }
    .k-card,
    .k-card * {
        direction: rtl !important;
        text-align: right !important;
    }
    .k-card {
        background: rgba(13, 27, 48, 0.65);
        backdrop-filter: blur(16px);
        border: 1px solid rgba(56, 189, 248, 0.28);
        border-right: 4px solid #00f2fe;
        border-radius: 14px;
        padding: 20px 22px;
        margin-bottom: 12px;
        box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.4),
            inset 0 0 15px rgba(56, 189, 248, 0.05);
        transition: all 0.3s ease;
    }
    .k-card:hover {
        border-color: rgba(56, 189, 248, 0.65);
        box-shadow:
            0 10px 35px rgba(0, 0, 0, 0.55),
            0 0 22px rgba(0, 242, 254, 0.22);
    }
    .k-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        direction: rtl !important;
        text-align: right !important;
    }
    .k-title {
        color: #ffffff;
        font-size: 1.25rem;
        font-weight: 700;
        margin: 10px 0;
        text-shadow: 0 0 12px rgba(56, 189, 248, 0.35);
        direction: rtl !important;
        text-align: right !important;
    }
    .k-tag {
        display: inline-block;
        background: rgba(0, 242, 254, 0.12);
        color: #67e8f9;
        border: 1px solid rgba(0, 242, 254, 0.45);
        box-shadow: 0 0 10px rgba(0, 242, 254, 0.2);
        border-radius: 20px;
        padding: 4px 12px;
        font-size: 0.8rem;
        font-weight: 700;
        margin: 0;
        max-width: 100%;
        white-space: normal;
        overflow-wrap: anywhere;
        direction: rtl !important;
        text-align: right !important;
    }
    .k-section {
        background: rgba(7, 17, 34, 0.75);
        border: 1px solid rgba(56, 189, 248, 0.3);
        border-right: 3px solid #38bdf8;
        padding: 10px 14px;
        border-radius: 10px;
        margin-top: 10px;
        font-size: 0.95rem;
        color: #e2e8f0;
        overflow-wrap: anywhere;
        box-shadow: inset 0 0 12px rgba(56, 189, 248, 0.08);
        direction: rtl !important;
        text-align: right !important;
    }
    .stTextInput > div > div,
    .stSelectbox > div > div,
    .stTextArea > div > div,
    [data-testid="stFileUploader"] section {
        background: rgba(10, 22, 40, 0.75) !important;
        border: 1px solid rgba(56, 189, 248, 0.3) !important;
        border-radius: 10px !important;
        color: #f8fafc !important;
        box-shadow: inset 0 0 10px rgba(56, 189, 248, 0.05) !important;
    }
    .stTextInput > div > div:focus-within,
    .stSelectbox > div > div:focus-within,
    .stTextArea > div > div:focus-within {
        border-color: #00f2fe !important;
        box-shadow: 0 0 15px rgba(0, 242, 254, 0.35) !important;
    }
    [data-testid="stWidgetLabel"] p,
    [data-testid="stRadio"] label,
    [data-testid="stExpander"] summary {
        text-align: right;
    }
    [data-testid="stRadio"] [role="radiogroup"] {
        direction: rtl;
        justify-content: flex-start;
        gap: 8px;
    }
    div[data-testid="stExpander"] {
        background: rgba(13, 27, 48, 0.6) !important;
        backdrop-filter: blur(14px) !important;
        border: 1px solid rgba(56, 189, 248, 0.28) !important;
        border-radius: 12px !important;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3) !important;
    }
    [data-testid="stMain"] .stButton > button,
    [data-testid="stSidebar"] .stButton > button,
    [data-testid="stMain"] .stFormSubmitButton > button,
    [data-testid="stMain"] [data-testid="stFileUploader"] button,
    [data-testid="stSidebar"] button[data-testid^="stBaseButton"] {
        background: linear-gradient(135deg, rgba(14, 165, 233, 0.22) 0%, rgba(3, 105, 161, 0.35) 100%) !important;
        color: #e0f2fe !important;
        border: 1px solid rgba(56, 189, 248, 0.45) !important;
        border-radius: 10px !important;
        font-weight: 700 !important;
        box-shadow: 0 0 14px rgba(14, 165, 233, 0.18) !important;
        transition: all 0.25s ease !important;
    }
    [data-testid="stMain"] .stButton > button:hover,
    [data-testid="stSidebar"] .stButton > button:hover,
    [data-testid="stMain"] .stFormSubmitButton > button:hover,
    [data-testid="stMain"] [data-testid="stFileUploader"] button:hover,
    [data-testid="stSidebar"] button[data-testid^="stBaseButton"]:hover {
        background: linear-gradient(135deg, rgba(0, 242, 254, 0.35) 0%, rgba(14, 165, 233, 0.5) 100%) !important;
        border-color: #00f2fe !important;
        color: #ffffff !important;
        box-shadow: 0 0 22px rgba(0, 242, 254, 0.45) !important;
        transform: translateY(-1px);
    }
    [data-testid="stAlert"] {
        direction: rtl;
        text-align: right;
        border-radius: 12px;
    }
    [data-testid="stSlider"] [role="group"] > div > div:nth-child(2) {
        background: #38bdf8 !important;
        box-shadow: 0 0 12px rgba(56, 189, 248, 0.75) !important;
    }
    img, video, canvas, iframe {
        max-width: 100% !important;
        height: auto !important;
    }
    pre, code, [data-testid="stCode"] {
        white-space: pre-wrap !important;
        word-break: break-word !important;
    }
    @media (max-width: 820px) {
        .block-container {
            max-width: 100% !important;
            padding-left: 0.85rem !important;
            padding-right: 0.85rem !important;
            padding-top: 1rem !important;
        }
        section[data-testid="stSidebar"] {
            min-width: min(100vw, 320px) !important;
            width: min(100vw, 360px) !important;
        }
        section[data-testid="stSidebar"] input,
        section[data-testid="stSidebar"] textarea,
        [data-testid="stMain"] input,
        [data-testid="stMain"] textarea {
            font-size: 16px !important;
        }
        [data-testid="stHorizontalBlock"] {
            flex-direction: column !important;
            flex-wrap: nowrap !important;
            gap: 0.75rem !important;
        }
        [data-testid="stHorizontalBlock"] > div,
        [data-testid="stColumn"],
        [data-testid="column"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 0 !important;
        }
        [data-testid="stRadio"] [role="radiogroup"] {
            flex-direction: column !important;
            align-items: flex-start !important;
            gap: 0.35rem !important;
        }
        .hero-box {
            padding: 16px;
            border-radius: 14px;
        }
        .hero-box h1 {
            font-size: 1.45rem !important;
            line-height: 1.35 !important;
        }
        .hub-badge {
            letter-spacing: 0;
            white-space: normal;
        }
        .metric-card {
            margin-bottom: 0;
            padding: 12px;
        }
        .metric-val {
            font-size: 1.7rem;
        }
        .k-card {
            padding: 14px;
        }
        .k-title {
            font-size: 1.05rem;
        }
        .stButton > button {
            width: 100% !important;
        }
    }
</style>
""", unsafe_allow_html=True)

if not is_admin:
    st.markdown("""
<style>
    [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"],
    [data-testid="stExpandSidebarButton"] {
        display: none !important;
    }
    [data-testid="stAppViewContainer"],
    section[data-testid="stMain"] {
        margin-left: 0 !important;
        margin-right: 0 !important;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. دالة مساعدة لاستدعاء Groq داخل الواجهة
# ==========================================
def ask_groq_safe(prompt_text: str, api_key: str) -> str:
    from database_and_ai import _groq_complete

    completion = _groq_complete(api_key, [{"role": "user", "content": prompt_text}], temperature=0.2)
    return completion.choices[0].message.content

# ==========================================
# 3. البيانات النموذجية
# ==========================================
def seed_sample_data():
    samples = [
        {
            "id": 90001,
            "category": "الأعطال والتشخيص",
            "car_model": "Jaecoo J7",
            "sub_system": "العضلات والفرامل والتعليق",
            "title": "صوت طقطقة خفيف في المطبات الأمامية",
            "problem_or_topic": "ظهور صوت طقطقة خفيفة أسفل مقدمة السيارة عند المرور على المطبات الصغيرة.",
            "solution_or_summary": "جاري التشخيص بين جلد عمود التوازن أو كراسي المساعدات، وينصح بفحصها وتغييرها على الضمان.",
            "part_numbers_or_specs": "فحص جلد عمود التوازن (Sway Bar Bushings)",
            "is_verified_solution": True,
            "keywords": "طقطقة, مطبات, مساعدات, عمود التوازن",
            "author": "عضو القروب",
        },
        {
            "id": 90002,
            "category": "الإصلاحات والحلول المؤكدة",
            "car_model": "Jaecoo J8",
            "sub_system": "الكهرباء والبطارية والحساسات",
            "title": "حل تنبيه تفريغ البطارية وتحديث النظام",
            "problem_or_topic": "ظهور رسالة انخفاض طاقة البطارية عند توقف السيارة لعدة أيام.",
            "solution_or_summary": "فحص البطارية بقياس الجهد والتأكد من سلامة بطارية AGM وتحديث برمجة وحدة التحكم بالوكالة.",
            "part_numbers_or_specs": "بطارية AGM 70Ah",
            "is_verified_solution": True,
            "keywords": "بطارية, AGM, تفريغ, كهرباء",
            "author": "أبو إياد",
        },
    ]
    for s in samples:
        save_card(s, s["id"], "", s["author"], s["problem_or_topic"])

# ==========================================
# 4. القائمة الجانبية (مركز التحكم والربط)
# ==========================================
if is_admin:
    with st.sidebar:
        st.markdown("### 💠 مركز التحكم والربط")

        keys_ready = bool(str(cfg.get("groq_api_key") or "").strip() and str(cfg.get("tg_api_id") or "").strip())
        if keys_ready:
            st.caption("مفاتيح Groq وTelegram محمّلة من الإعدادات المحفوظة.")
        _load_settings_into_session(cfg)
        with st.expander("🔐 إعدادات المفاتيح والربط بالتلجرام (حفظ دائم)", expanded=not keys_ready):
            groq_key_input = st.text_input(
                "مفتاح Groq API",
                key="settings_groq",
                type="password",
                help="أدخل مفتاح Groq الخاص بك (يبدأ بـ gsk_)",
            )
            tg_id_input = st.text_input("Telegram API ID", key="settings_tg_id")
            tg_hash_input = st.text_input("Telegram API Hash", key="settings_tg_hash", type="password")
            tg_group_input = st.text_input("رابط أو معرف القروب (اختياري)", key="settings_tg_group")
            tg_session_input = st.text_input(
                "جلسة السحابة النصية tg_session (اختياري)",
                key="settings_tg_session",
                type="password",
                help="على Streamlit Cloud تُقرأ تلقائياً من st.secrets باسم TELEGRAM_SESSION.",
            )
            bot_token_input = st.text_input(
                "Bot Token (اختياري)",
                key="settings_bot_token",
                type="password",
                help="غير مطلوب إذا كان API ID و Hash محفوظين مع جلسة نصية أو ملف جلسة.",
            )
            auto_reply_input = st.checkbox("تفعيل الرد الآلي في التلجرام", key="settings_auto_reply")

            if st.button("حفظ الإعدادات", use_container_width=True):
                cfg["groq_api_key"] = groq_key_input.strip()
                cfg["tg_api_id"] = tg_id_input.strip()
                cfg["tg_api_hash"] = tg_hash_input.strip()
                cfg["tg_group"] = tg_group_input.strip()
                cfg["tg_session"] = tg_session_input.strip()
                cfg["bot_token"] = bot_token_input.strip()
                cfg["auto_reply_enabled"] = auto_reply_input
                save_config(cfg)
                st.session_state["_settings_fp"] = _settings_fingerprint(cfg)
                st.success("✨ تم حفظ إعدادات Groq والتيليجرام بنجاح!")

# ==========================================
# 5. الواجهة الرئيسية وعرض البطاقات
# ==========================================
if is_admin:
    auto_sync_status()
    auto_result = st.session_state.get("auto_sync_result")
    if auto_result and auto_result.get("ok"):
        st.caption(
            f"آخر تحديث تلقائي من {auto_result.get('group') or 'القروب'}: "
            f"حُفظت {auto_result.get('saved', 0)} بطاقة."
        )

st.markdown("""
<div class="hero-box">
    <div class="hub-badge">JAECOO AI DIAGNOSTICS HUB</div>
    <h1>💠 منصة جايكو الذكية</h1>
    <p>منظومة ذكاء اصطناعي لتحليل وأرشفة الخبرات الفنية والأعطال والحلول لقروب ملاك جايكو</p>
</div>
""", unsafe_allow_html=True)

conn = get_db_connection()
df = pd.read_sql_query("SELECT * FROM knowledge_cards ORDER BY id DESC", conn)
conn.close()

def _model_count(frame: pd.DataFrame, model: str) -> int:
    if frame.empty or "car_model" not in frame.columns:
        return 0
    return int(frame["car_model"].fillna("").str.strip().str.upper().eq(model.upper()).sum())


def _render_stat_card(title: str, count: int, icon: str) -> None:
    st.markdown(
        f'<div class="metric-card"><div>{icon} {html.escape(title)}</div><div class="metric-val">{int(count)}</div></div>',
        unsafe_allow_html=True,
    )


def _render_metrics(frame: pd.DataFrame) -> None:
    total_cards = 0 if frame.empty else len(frame)
    if frame.empty or "is_verified_solution" not in frame.columns:
        verified_cards = 0
    else:
        verified_cards = int(frame["is_verified_solution"].fillna(0).astype(int).eq(1).sum())
    col_total, col_verified = st.columns(2)
    with col_total:
        _render_stat_card("إجمالي البطاقات", total_cards, "🔹")
    with col_verified:
        _render_stat_card("الحلول المؤكدة", verified_cards, "✅")
    col_j8, col_j8_shs, col_j7, col_j5 = st.columns(4)
    with col_j8:
        _render_stat_card("بطاقات Jaecoo J8", _model_count(frame, "Jaecoo J8"), "🚙")
    with col_j8_shs:
        _render_stat_card("بطاقات Jaecoo J8 SHS", _model_count(frame, "Jaecoo J8 SHS"), "⚡")
    with col_j7:
        _render_stat_card("بطاقات Jaecoo J7", _model_count(frame, "Jaecoo J7"), "🚘")
    with col_j5:
        _render_stat_card("بطاقات Jaecoo J5", _model_count(frame, "Jaecoo J5"), "🚗")

metrics_box = st.empty()
with metrics_box.container():
    _render_metrics(df)

if st.session_state.get("sync_notice"):
    st.success(st.session_state.pop("sync_notice"))

with st.expander("🎙️📷 مختبر التحليل الذكي", expanded=False):
    lab_text = st.text_area("النص المراد تحليله", key="public_lab_text", height=120)
    lab_file = st.file_uploader(
        "مرفق اختياري",
        type=["png", "jpg", "jpeg", "mp3", "wav", "ogg", "m4a", "mp4"],
        key="public_lab_file",
    )

limit_val = st.slider("عدد الرسائل", min_value=1, max_value=50, value=30, step=1, key="public_pull_limit_cap")
sync_feedback = st.empty()
if st.session_state.get("sync_logs"):
    with st.expander("سجل السحب الحي", expanded=True):
        st.code("\n".join(st.session_state["sync_logs"]), language="text")
pull_col, analyze_col, sample_col = st.columns(3)
with pull_col:
    if st.button("سحب رسائل القروب", use_container_width=True, key="public_pull"):
        opening = (
            "جاري الاتصال بملف الجلسة المحلي..."
            if os.path.exists(PERSONAL_SESSION_FILE)
            else "جاري الاتصال بجلسة الحساب..."
        )
        if not _SYNC_STATE["running"]:
            with _SYNC_GUARD:
                _SYNC_STATE["running"] = True
                _SYNC_STATE["result"] = None
                _SYNC_STATE["lines"] = [opening]
                _SYNC_STATE["fraction"] = 0.08
                _SYNC_STATE["message"] = opening

            def _pull_worker() -> None:
                try:
                    result = run_sync(int(limit_val), on_progress=_record_sync_progress)
                except Exception as exc:
                    result = {"ok": False, "saved": 0, "error": str(exc), "logs": []}
                with _SYNC_GUARD:
                    _SYNC_STATE["result"] = result
                    _SYNC_STATE["done_at"] = time.time()
                    _SYNC_STATE["running"] = False

            threading.Thread(target=_pull_worker, daemon=True).start()

        with sync_feedback.container():
            bar = st.progress(0.08, text=opening)
            log_slot = st.empty()
            with st.spinner("جاري سحب رسائل القروب..."):
                while True:
                    with _SYNC_GUARD:
                        running = _SYNC_STATE["running"]
                        lines = list(_SYNC_STATE["lines"])
                        fraction = float(_SYNC_STATE["fraction"] or 0.0)
                        message = str(_SYNC_STATE["message"] or "جاري السحب...")
                        result = _SYNC_STATE["result"]
                    log_slot.code("\n".join(lines) or message, language="text")
                    bar.progress(min(1.0, max(0.0, fraction)), text=message)
                    if not running:
                        break
                    time.sleep(0.4)
            result = result or {"ok": False, "error": "تعذر إكمال السحب.", "saved": 0}
            fresh = get_db_connection()
            fresh_df = pd.read_sql_query("SELECT * FROM knowledge_cards ORDER BY id DESC", fresh)
            fresh.close()
            total_cards = len(fresh_df)
            if result.get("ok"):
                lines.append(
                    f"اكتمل السحب. الرسائل المفحوصة: {result.get('scanned', 0)}، "
                    f"البطاقات المحفوظة في هذه الجولة: {result.get('saved', 0)}، "
                    f"إجمالي القاعدة: {total_cards}."
                )
                bar.progress(1.0, text=f"اكتمل السحب. إجمالي البطاقات: {total_cards}")
            else:
                lines.append(result.get("error") or "تعذر إكمال السحب.")
                bar.progress(1.0, text="انتهى السحب")
            log_slot.code("\n".join(lines), language="text")
        st.session_state["sync_logs"] = lines
        if result.get("ok"):
            with metrics_box.container():
                _render_metrics(fresh_df)
            st.session_state["sync_notice"] = (
                f"تم سحب قروب {result.get('group') or 'التلجرام'} "
                f"وحفظ {result.get('saved', 0)} بطاقة. الإجمالي الآن {total_cards}."
            )
            st.rerun()
with analyze_col:
    if st.button("بدء التحليل", use_container_width=True, key="public_analyze"):
        groq_key = cfg.get("groq_api_key") or cfg.get("gemini_api_key")
        if not lab_text and not lab_file:
            st.warning("أدخل النص في مختبر التحليل الذكي.")
        elif not groq_key:
            st.warning("مفتاح التحليل غير محفوظ في إعدادات التطبيق.")
        else:
            with st.spinner("جاري التحليل..."):
                try:
                    source_text, media_type, media_b64 = _prepare_lab_source(lab_text, lab_file, groq_key)
                    card = analyze_thread_with_ai(source_text or "حلل المرفق", groq_key)
                    msg_id = int(pd.Timestamp.now().timestamp())
                    save_card(card, msg_id, "", "مختبر التحليل", source_text or "مرفق وسائط", media_type, media_b64)
                    st.session_state["sync_notice"] = f"تم حفظ البطاقة: {card.get('title')}"
                    st.rerun()
                except Exception as e:
                    st.error(f"خطأ أثناء التحليل: {e}")
with sample_col:
    if st.button("استعادة البيانات النموذجية", use_container_width=True, key="public_seed"):
        seed_sample_data()
        st.session_state["sync_notice"] = "تمت استعادة البيانات النموذجية!"
        st.rerun()

with st.expander("➕ إضافة بطاقة معرفية يدوياً", expanded=False):
    m_title = st.text_input("عنوان البطاقة", key="manual_title")
    m_model = st.selectbox(
        "الطراز",
        JAECOO_MODELS,
        key="manual_model",
    )
    m_cat = st.selectbox("القسم", [
        "الأعطال والتشخيص",
        "الإصلاحات والحلول المؤكدة",
        "بنك القطع البديلة والمطابقات",
        "الأنظمة الذكية والشاشات",
        "الصيانة الدورية والوكالات",
        "الإكسسوارات والتجهيزات",
        "تجارب القيادة والأداء الفعلي",
        "نبض القروب والدردشة العامة",
    ], key="manual_cat")
    m_sub = st.selectbox("النظام الفرعي", [
        "الوكيل والمراكز المعتمدة",
        "جدول وتكاليف الصيانة الدورية",
        "المحرك والتيربو",
        "القير والدفع الرباعي AWD",
        "الكهرباء والبطارية والحساسات",
        "الشاشة والنظام الصوتي والربط",
        "التكييف والتبريد",
        "العضلات والفرامل والتعليق",
        "الهيكل الخارجي والمقصورة",
        "عام",
    ], key="manual_sub")
    m_prob = st.text_area("الوصف / المشكلة", key="manual_prob")
    m_sol = st.text_area("الخلاصة / الحل", key="manual_sol")
    m_parts = st.text_input("القطع والمواصفات الفنية", key="manual_parts")
    m_image = st.file_uploader("صورة مرفقة اختيارية", type=["png", "jpg", "jpeg"], key="manual_image")
    m_ver = st.checkbox("حل مؤكد ومجرب", value=True, key="manual_verified")
    if st.button("حفظ البطاقة اليدوية", use_container_width=True, key="manual_save"):
        if m_title and m_sol:
            card_manual = {
                "category": m_cat,
                "car_model": m_model,
                "sub_system": m_sub,
                "title": m_title,
                "problem_or_topic": m_prob,
                "solution_or_summary": m_sol,
                "part_numbers_or_specs": m_parts,
                "is_verified_solution": m_ver,
                "keywords": m_title,
            }
            msg_id = int(pd.Timestamp.now().timestamp())
            manual_b64 = _image_preview(m_image.getvalue()) if m_image is not None else ""
            manual_type = "image" if manual_b64 else "text"
            save_card(card_manual, msg_id, "", "إضافة يدوية", m_prob, manual_type, manual_b64)
            st.session_state["sync_notice"] = "تمت إضافة البطاقة!"
            st.rerun()
        else:
            st.warning("أدخل عنوان البطاقة والخلاصة.")

st.write("")
col_s1, col_s2 = st.columns([2, 1])
with col_s1:
    search_q = st.text_input("🔍 البحث الفوري في قاعدة المعرفة", placeholder="ابحث عن: بطارية AGM، شاشة، تكييف، طقطقة...")
with col_s2:
    model_filter = st.selectbox(
        "🚙 تصفية حسب الطراز:",
        ["الكل", *JAECOO_MODELS],
    )

if is_admin:
  with st.expander("🤖 استشارة المهندس الذكي والموجز اليومي", expanded=False):
    user_q = st.text_input("اسأل المهندس الذكي عن أي عطل أو قطعة غيار:")
    c_btn1, c_btn2 = st.columns(2)
    with c_btn1:
        if st.button("بحث واستشارة فورية 💡", use_container_width=True):
            if not cfg.get("groq_api_key"):
                st.warning("أدخل مفتاح Groq API في الإعدادات أولاً.")
            elif user_q:
                with st.spinner("جاري فحص الأرشيف وتوليد الإجابة..."):
                    try:
                        db_ans = find_matching_solution_in_db(user_q, cfg["groq_api_key"])
                        if db_ans:
                            st.info(db_ans)
                        else:
                            ai_ans = ask_groq_safe(
                                f"أنت كبير مهندسي صيانة سيارات جايكو J7 و J8. أجب بدقة عن هذا الاستفسار:\n{user_q}",
                                cfg["groq_api_key"],
                            )
                            st.info(ai_ans)
                    except Exception as e:
                        st.error(f"حدث خطأ: {e}")
    with c_btn2:
        if st.button("⚡ توليد الموجز الذكي لنبض القروب", use_container_width=True):
            if not cfg.get("groq_api_key"):
                st.warning("أدخل مفتاح Groq API في الإعدادات أولاً.")
            else:
                with st.spinner("جاري صياغة الموجز..."):
                    try:
                        pulse = generate_daily_pulse_summary(cfg["groq_api_key"])
                        st.success(pulse)
                    except Exception as e:
                        st.error(f"حدث خطأ: {e}")

# تصفية البيانات
filtered_df = df.copy()
if model_filter != "الكل" and not filtered_df.empty:
    filtered_df = filtered_df[filtered_df["car_model"] == model_filter]

if search_q.strip() and not filtered_df.empty:
    q = search_q.strip()
    mask = (
        filtered_df["title"].fillna("").str.contains(q, case=False)
        | filtered_df["problem_or_topic"].fillna("").str.contains(q, case=False)
        | filtered_df["solution_or_summary"].fillna("").str.contains(q, case=False)
        | filtered_df["part_numbers_or_specs"].fillna("").str.contains(q, case=False)
        | filtered_df["keywords"].fillna("").str.contains(q, case=False)
    )
    filtered_df = filtered_df[mask]

categories = ["الكل", "الأعطال والتشخيص", "الإصلاحات والحلول المؤكدة", "بنك القطع البديلة والمطابقات", "الأنظمة الذكية والشاشات", "الصيانة الدورية والوكالات", "الإكسسوارات والتجهيزات"]
selected_cat = st.radio("الأقسام:", categories, horizontal=True)

if selected_cat != "الكل" and not filtered_df.empty:
    filtered_df = filtered_df[filtered_df["category"] == selected_cat]

media_counts = {}
if not filtered_df.empty and "media_type" in filtered_df.columns:
    media_counts = filtered_df["media_type"].fillna("text").value_counts().to_dict()
media_options = {
    "all": f"🌐 الكل ({len(filtered_df)})",
    "text": f"📝 نصوص ({int(media_counts.get('text', 0))})",
    "image": f"🖼️ صور ({int(media_counts.get('image', 0))})",
    "audio": f"🎙️ صوتيات ({int(media_counts.get('audio', 0))})",
    "video": f"🎥 فيديوهات ({int(media_counts.get('video', 0))})",
}
st.markdown("##### 🎯 تصفية حسب نوع الوسائط")
selected_media = st.radio(
    "نوع الوسائط",
    options=list(media_options.keys()),
    format_func=lambda key: media_options[key],
    horizontal=True,
    label_visibility="collapsed",
    key="media_type_filter",
)
if selected_media != "all" and not filtered_df.empty and "media_type" in filtered_df.columns:
    filtered_df = filtered_df[filtered_df["media_type"].fillna("text") == selected_media]

if filtered_df.empty:
    st.info("لا توجد بطاقات مطابقة حالياً. استخدم زر سحب رسائل القروب أو بدء التحليل في أعلى الصفحة.")
else:
    for _, row in filtered_df.iterrows():
        card_id = int(row["id"])
        title_safe = html.escape(_card_text(row.get("title")))
        prob_safe = html.escape(_card_text(row.get("problem_or_topic")))
        sol_safe = html.escape(_card_text(row.get("solution_or_summary")))
        parts_raw = _card_text(row.get("part_numbers_or_specs"))
        parts_safe = html.escape(parts_raw)
        car_safe = html.escape(_card_text(row.get("car_model")))
        sub_safe = html.escape(_card_text(row.get("sub_system")))
        cat_safe = html.escape(_card_text(row.get("category")))
        media_key = str(row.get("media_type") or "text").strip().lower()
        badge = MEDIA_BADGES.get(media_key, MEDIA_BADGES["text"])
        media_html = (
            f'<span class="k-tag" style="color:{badge["color"]};border-color:{badge["color"]}">'
            f'{badge["icon"]} {html.escape(badge["label"])}</span>'
        )

        parts_html = (
            f'<div class="k-section">🔧 <b>القطع والمواصفات الفنية:</b> {parts_safe}</div>'
            if parts_raw and parts_raw != "غير محدد"
            else ""
        )

        st.markdown(f"""
        <div class="k-card">
            <div class="k-tags">
                <span class="k-tag">🚙 {car_safe}</span>
                <span class="k-tag">⚙️ {sub_safe}</span>
                <span class="k-tag">📂 {cat_safe}</span>
                {media_html}
            </div>
            <div class="k-title">{title_safe}</div>
            <div class="k-section">🔹 <b>الوصف / المشكلة:</b> {prob_safe}</div>
            <div class="k-section">⚡ <b>الخلاصة / الحل:</b> {sol_safe}</div>
            {parts_html}
        </div>
        """, unsafe_allow_html=True)
        preview_b64 = _card_text(row.get("media_b64"))
        if preview_b64:
            with st.expander("🖼️ عرض الصورة المرفقة مع البطاقة"):
                enlarge = st.toggle("تكبير الصورة", key=f"zoom_card_{card_id}")
                st.image(base64.b64decode(preview_b64), use_container_width=enlarge)
        if st.button(f"🗑️ حذف البطاقة #{card_id}", key=f"delete_card_{card_id}"):
            delete_knowledge_card(card_id)
            st.rerun()