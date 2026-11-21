import base64
import hmac
import html
import os
import re
import secrets
import sqlite3
from pathlib import Path
import threading
import time
from datetime import timedelta

import pandas as pd
import streamlit as st

from database_and_ai import (
    analyze_image_bytes,
    analyze_thread_with_ai,
    clean_groq_key,
    describe_groq_error,
    test_groq_key,
    compress_image_to_b64,
    DB_NAME,
    find_matching_solution_in_db,
    generate_daily_pulse_summary,
    get_cards_media_map,
    get_db_connection,
    init_db,
    list_maintenance_rows,
    list_spare_parts,
    load_config,
    load_telegram_session,
    save_card,
    save_config,
    transcribe_media_bytes,
)
from telegram_scraper import (
    PERSONAL_SESSION_FILE,
    confirm_telegram_login,
    request_telegram_code,
    clear_stop_sync,
    request_stop_sync,
    reset_sync_checkpoint,
    run_sync,
)

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
CARD_PAGE_SIZE = 25
CONSULT_COOLDOWN_SECONDS = 10
_SYNC_LINE_LIMIT = 80
_URL_TRAILING = ".,:;!?،؛])"
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
    """يزيل nan والبطاقات الإنجليزية العشوائية ويوحّد مسمى النظام الفرعي عند فتح التطبيق."""
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
            SET sub_system = 'المحرك والتيربو والمنظومة الهجين SHS'
            WHERE sub_system = 'المحرك والتيربو'
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


def _load_cards_dataframe() -> pd.DataFrame:
    """يجلب بيانات البطاقات الخفيفة للتصفح السريع دون تحميل الصور الثقيلة لكل الأرشيف."""
    conn = get_db_connection()
    try:
        return pd.read_sql_query(
            """
            SELECT
                id, telegram_msg_id, thread_url, category, car_model, sub_system,
                title, problem_or_topic, solution_or_summary, part_numbers_or_specs,
                is_verified_solution, keywords, author_name, created_at, media_type
            FROM knowledge_cards
            ORDER BY id DESC
            """,
            conn,
        )
    finally:
        conn.close()


def delete_knowledge_card(card_id: int) -> None:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM knowledge_cards WHERE id = ?", (int(card_id),))
        conn.commit()
    finally:
        conn.close()


def _restore_database(payload: bytes) -> str:
    if not payload.startswith(b"SQLite format 3"):
        return "الملف ليس قاعدة SQLite صالحة."
    temp_path = Path(DB_NAME).with_name("jaecoo_restore_upload.db")
    temp_path.write_bytes(payload)
    incoming = None
    live = None
    try:
        incoming = sqlite3.connect(temp_path)
        incoming.execute("SELECT COUNT(*) FROM knowledge_cards")
        live = get_db_connection()
        incoming.backup(live)
        return ""
    except Exception as exc:
        return f"تعذرت الاستعادة: {exc}"
    finally:
        if live is not None:
            live.close()
        if incoming is not None:
            incoming.close()
        temp_path.unlink(missing_ok=True)


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
    media_bytes = lab_file.getvalue()
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


def format_clickable_links(text: str) -> str:
    """يحوّل روابط علي إكسبريس وغيرها إلى روابط قابلة للضغط بعد تهريب النص."""
    if not text:
        return ""
    pattern = re.compile(r"https?://[^\s<>\"]+")
    parts = []
    last = 0
    for match in pattern.finditer(text):
        parts.append(html.escape(text[last:match.start()]))
        url = match.group(0).rstrip(_URL_TRAILING)
        safe_url = html.escape(url, quote=True)
        label = "🛒 اضغط هنا لفتح رابط المنتج (AliExpress)" if "aliexpress" in url.lower() else "🔗 فتح الرابط"
        parts.append(
            f'<a href="{safe_url}" target="_blank" style="color:#00e5ff; font-weight:bold; text-decoration:underline;">{label}</a>'
        )
        last = match.start() + len(url)
    parts.append(html.escape(text[last:]))
    return "".join(parts).replace("\n", "<br>")


def _normalize_arabic(text: str) -> str:
    folded = str(text or "")
    for source, target in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        folded = folded.replace(source, target)
    return folded


def _decode_card_image(payload: str) -> bytes | None:
    text = _card_text(payload)
    if not text:
        return None
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except Exception:
        return None


def _card_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _telegram_login_needed(config: dict) -> bool:
    from database_and_ai import get_app_setting
    if st.session_state.get("tg_phone_code_hash"):
        return True
    current_session = _clean_tg_session(
        st.session_state.get("tg_active_session")
        or read_cloud_string_session()
        or load_telegram_session()
        or config.get("tg_session")
        or ""
    )
    if not current_session:
        return True
    known_bad = _clean_tg_session(get_app_setting("invalid_tg_session"))
    return bool(known_bad and known_bad == current_session)


def render_telegram_login(config: dict, expanded: bool = False) -> None:
    with st.expander("🔑 تفعيل أو تجديد جلسة تيليجرام مباشرة", expanded=expanded):
        st.caption("أدخل رقم الجوال بالصيغة الدولية. يصل رمز التحقق داخل تطبيق تيليجرام.")
        phone = st.text_input("رقم الجوال", key="tg_phone", placeholder="+9665xxxxxxx")
        if st.button("إرسال رمز التحقق", key="tg_send_code"):
            result = request_telegram_code(phone)
            if result.get("ok"):
                st.session_state["tg_phone_code_hash"] = result.get("phone_code_hash", "")
                st.session_state["tg_temp_session"] = result.get("temp_session", "")
                st.session_state["tg_phone_confirmed"] = phone.strip().replace(" ", "")
                st.success("أُرسل رمز التحقق إلى تطبيق تيليجرام.")
            else:
                st.error(result.get("error") or "تعذر إرسال الرمز.")
        if st.session_state.get("tg_phone_code_hash"):
            st.text_input("رمز التحقق", key="tg_code")
            st.text_input("كلمة مرور التحقق بخطوتين (إن وجدت)", type="password", key="tg_password")
            if st.session_state.get("tg_needs_password"):
                st.warning("الحساب يطلب كلمة مرور التحقق بخطوتين.")
            if st.button("تفعيل الجلسة", key="tg_confirm_login"):
                result = confirm_telegram_login(
                    st.session_state.get("tg_phone_confirmed") or phone,
                    st.session_state.get("tg_code", ""),
                    st.session_state.get("tg_phone_code_hash", ""),
                    st.session_state.get("tg_temp_session", ""),
                    st.session_state.get("tg_password", ""),
                )
                if result.get("needs_password"):
                    st.session_state["tg_needs_password"] = True
                    if result.get("temp_session"):
                        st.session_state["tg_temp_session"] = result["temp_session"]
                    st.warning(result.get("error") or "أدخل كلمة مرور التحقق بخطوتين.")
                elif result.get("ok"):
                    st.session_state["tg_active_session"] = result.get("session", "")
                    st.session_state["tg_phone_code_hash"] = ""
                    st.session_state["tg_needs_password"] = False
                    config["tg_session"] = result.get("session", "")
                    os.environ["TELEGRAM_SESSION"] = result.get("session", "")
                    st.success("تم التفعيل. انسخ الكود إلى TELEGRAM_SESSION في Secrets. السحب يستخدمه الآن.")
                else:
                    st.error(result.get("error") or "تعذر التفعيل.")
        active_session = str(st.session_state.get("tg_active_session") or "").strip()
        if active_session:
            st.text_area("TELEGRAM_SESSION", value=active_session, height=140)


def _trim_sync_lines() -> None:
    extra = len(_SYNC_STATE["lines"]) - _SYNC_LINE_LIMIT
    if extra > 0:
        del _SYNC_STATE["lines"][:extra]


def _record_sync_progress(message: str, fraction: float) -> None:
    with _SYNC_GUARD:
        if not (_SYNC_STATE["lines"] and _SYNC_STATE["lines"][-1] == message):
            _SYNC_STATE["lines"].append(message)
            _trim_sync_lines()
        _SYNC_STATE["fraction"] = fraction
        _SYNC_STATE["message"] = message


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


def hide_streamlit_cloud_badges() -> None:
    """يخفي شارات Streamlit Cloud بحقن نمط واحد، ويبقي زر القائمة الجانبية للمشرف."""
    st.markdown(
        """
        <style>
            #MainMenu,
            [data-testid="stToolbar"],
            [data-testid="viewerBadge"],
            [data-testid="profileContainer"],
            footer {
                display: none !important;
                visibility: hidden !important;
            }
            header[data-testid="stHeader"] {
                background: transparent !important;
                height: 0 !important;
                min-height: 0 !important;
            }
            section[data-testid="stSidebar"],
            [data-testid="stSidebarResizeHandle"],
            [data-testid="collapsedControl"],
            [data-testid="stExpandSidebarButton"] {
                display: none !important;
                visibility: hidden !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


hide_streamlit_cloud_badges()


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


@st.cache_resource
def _boot_database(_title: str) -> bool:
    """يشغّل تهيئة القاعدة والتنظيف وعنوان المشاركة مرة واحدة لكل عملية سيرفر."""
    init_db()
    clean_database()
    _publish_share_title(_title)
    return True


_boot_database(PAGE_TITLE)

_SETTING_SOURCES = {
    "groq_api_key": ("groq_api_key", "GROQ_API_KEY", "gemini_api_key"),
    "groq_api_key_2": ("groq_api_key_2", "GROQ_API_KEY_2"),
    "groq_api_key_3": ("groq_api_key_3", "GROQ_API_KEY_3"),
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


def _clean_tg_session(raw: str) -> str:
    text = str(raw or "").strip()
    if "=" in text and text.upper().startswith("TELEGRAM_SESSION"):
        text = text.split("=", 1)[1].strip()
    quotes = "\"'`“”‘’"
    while len(text) >= 2 and text[0] in quotes and text[-1] in quotes:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def read_cloud_string_session() -> str:
    """يبحث عن TELEGRAM_SESSION في الجذر وفي كل أقسام Secrets وينظفه من المسافات والأسطر."""
    target_keys = ("TELEGRAM_SESSION", "tg_session", "telegram_session", "TG_SESSION", "SESSION_STRING")
    try:
        secrets_obj = st.secrets
    except Exception:
        return ""
    for key in target_keys:
        try:
            val = _clean_tg_session(secrets_obj.get(key, ""))
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
                for key in target_keys:
                    val = _clean_tg_session(bag.get(key, ""))
                    if val:
                        return val
        except Exception:
            continue
    return ""


def apply_cloud_string_session(cfg: dict) -> dict:
    from database_and_ai import set_app_setting
    just_activated = _clean_tg_session(st.session_state.get("tg_active_session", ""))
    if just_activated:
        cfg["tg_session"] = just_activated
        os.environ["TELEGRAM_SESSION"] = just_activated
        return cfg
    secret = read_cloud_string_session()
    if secret:
        stored_db = _clean_tg_session(load_telegram_session())
        if secret != stored_db:
            set_app_setting("telegram_string_session", secret)
            set_app_setting("telegram_session_status", "ok")
            set_app_setting("invalid_tg_session", "")
        cfg["tg_session"] = secret
        os.environ["TELEGRAM_SESSION"] = secret
        return cfg
    local_saved = _clean_tg_session(load_telegram_session() or cfg.get("tg_session") or "")
    if local_saved:
        cfg["tg_session"] = local_saved
        os.environ["TELEGRAM_SESSION"] = local_saved
    return cfg


def hydrate_config(cfg: dict) -> dict:
    """يعيد قراءة config.json و st.secrets في كل تحميل ويحفظ المفاتيح الناقصة."""
    changed = False
    for key, names in _SETTING_SOURCES.items():
        current = clean_groq_key(cfg.get(key)) if key.startswith("groq_") else str(cfg.get(key) or "").strip()
        saved = _read_saved(cfg, names)
        secret = _read_secret(names)
        if key.startswith("groq_"):
            saved = clean_groq_key(saved)
            secret = clean_groq_key(secret)
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
    publish_groq_env(cfg)
    return cfg


def publish_groq_env(cfg: dict) -> None:
    """يقرأ مفاتيح Groq من st.secrets في الخيط الرئيسي ويضعها في os.environ."""
    mapping = (
        ("groq_api_key", "GROQ_API_KEY", ("GROQ_API_KEY", "groq_api_key", "gemini_api_key")),
        ("groq_api_key_2", "GROQ_API_KEY_2", ("GROQ_API_KEY_2", "groq_api_key_2")),
        ("groq_api_key_3", "GROQ_API_KEY_3", ("GROQ_API_KEY_3", "groq_api_key_3")),
    )
    for cfg_key, env_key, secret_names in mapping:
        secret = clean_groq_key(_read_secret(secret_names))
        saved = clean_groq_key(cfg.get(cfg_key))
        value = secret or saved
        if value.startswith("gsk_"):
            os.environ[env_key] = value
            cfg[cfg_key] = value


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


def _admin_secrets(cfg: dict) -> list[str]:
    values = []
    for item in (
        _read_secret(("admin_token", "ADMIN_TOKEN", "ADMIN_PIN")),
        str(cfg.get("admin_token") or ""),
    ):
        text = str(item or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _pin_matches(pin: str, cfg: dict) -> bool:
    provided = str(pin or "").strip()
    if not provided:
        return False
    for expected in _admin_secrets(cfg):
        if len(provided) == len(expected) and hmac.compare_digest(provided, expected):
            return True
    return False


def ensure_admin_token(cfg: dict) -> dict:
    secret = _read_secret(("admin_token", "ADMIN_TOKEN", "ADMIN_PIN"))
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
    provided = st.query_params.get("admin", "")
    if isinstance(provided, (list, tuple)):
        provided = provided[0] if provided else ""
    if not _pin_matches(str(provided or ""), cfg):
        return False
    st.session_state.is_admin = True
    try:
        del st.query_params["admin"]
    except Exception:
        pass
    return True


cfg = apply_cloud_string_session(ensure_admin_token(hydrate_config(load_config())))
is_admin = viewer_is_admin(cfg)

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
    with st.expander("💠 مركز التحكم والإعدادات والنسخ الاحتياطي (للمشرف)", expanded=False):
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
                publish_groq_env(cfg)
                st.session_state["_settings_fp"] = _settings_fingerprint(cfg)
                st.success("✨ تم حفظ إعدادات Groq والتيليجرام بنجاح!")
            if st.button("🩺 اختبار مفتاح Groq", use_container_width=True, key="test_groq_key"):
                trial_key = groq_key_input.strip() or str(cfg.get("groq_api_key") or "")
                if trial_key:
                    os.environ["GROQ_API_KEY"] = trial_key
                publish_groq_env(cfg)
                try:
                    test_groq_key(trial_key or os.environ.get("GROQ_API_KEY") or "")
                    st.success("✅ مفتاح Groq يعمل بنجاح")
                except Exception as exc:
                    st.error(describe_groq_error(exc))

        st.markdown("#### 💾 النسخ الاحتياطي لقاعدة المعرفة")
        db_path = Path(DB_NAME)
        if db_path.exists():
            st.download_button(
                "📥 تنزيل نسخة احتياطية كاملة من قاعدة البيانات (.db)",
                data=db_path.read_bytes(),
                file_name="jaecoo_knowledge.db",
                mime="application/octet-stream",
                use_container_width=True,
                key="download_db",
            )
        backup_conn = get_db_connection()
        try:
            backup_frame = pd.read_sql_query("SELECT * FROM knowledge_cards", backup_conn)
        finally:
            backup_conn.close()
        st.download_button(
            "تنزيل البطاقات (JSON)",
            data=backup_frame.to_json(orient="records", force_ascii=False),
            file_name="knowledge_cards.json",
            mime="application/json",
            use_container_width=True,
            key="download_cards_json",
        )
        restore_file = st.file_uploader("📤 استعادة ملف قاعدة البيانات (.db)", type=["db"], key="restore_db_file")
        if st.button("📤 استعادة ملف قاعدة البيانات (.db)", use_container_width=True, key="restore_db_button"):
            if restore_file is None:
                st.warning("اختر ملف قاعدة البيانات أولاً.")
            else:
                restore_error = _restore_database(restore_file.getvalue())
                if restore_error:
                    st.error(restore_error)
                else:
                    st.session_state["sync_notice"] = "تمت استعادة قاعدة المعرفة."
                    st.rerun()

# ==========================================
# 5. الواجهة الرئيسية وعرض البطاقات
# ==========================================
st.markdown("""
<div class="hero-box">
    <div class="hub-badge">JAECOO AI DIAGNOSTICS HUB</div>
    <h1>💠 منصة جايكو الذكية</h1>
    <p>منظومة ذكاء اصطناعي لتحليل وأرشفة الخبرات الفنية والأعطال والحلول لقروب ملاك جايكو</p>
</div>
""", unsafe_allow_html=True)

_SERVICE_MODELS = {
    "Jaecoo J8": "Jaecoo J8",
    "Jaecoo J8 SHS": "Jaecoo J7 PHEV",
    "Jaecoo J7": "Jaecoo J7",
    "Jaecoo J5": "Jaecoo J5",
}
_PART_MODELS = {
    "Jaecoo J8": "Jaecoo J7",
    "Jaecoo J8 SHS": "Jaecoo J7 PHEV",
    "Jaecoo J7": "Jaecoo J7",
    "Jaecoo J5": "Jaecoo J5",
}


def _render_service_reference() -> None:
    schedule_tab, parts_tab = st.tabs([
        "🛠️ جدول الصيانة الدورية والأسعار",
        "📦 أسعار القطع وأرقامها",
    ])
    with schedule_tab:
        chosen = st.selectbox(
            "الطراز",
            list(_SERVICE_MODELS),
            key="service_model_choice",
        )
        st.caption(
            "الأرقام من جدول OMODA | JAECOO الرسمي المنشور لعام 2026 بالرينغت الماليزي. "
            "الضريبة غير مفصولة داخل الوثيقة، لذلك لا يُضاف تقدير لضريبة السعودية. "
            "تجارب الملاك مثل ترصيص الهايبرد SHS تبقى في أرشيف القروب."
        )
        if chosen == "Jaecoo J8 SHS":
            st.info("لا يوجد جدول سعر مستقل لـ Jaecoo J8 SHS في الجداول المنشورة. المعروض هو جدول Jaecoo J7 PHEV، وهو أقرب جدول هايبرد رسمي منشور.")
        elif chosen == "Jaecoo J8":
            st.caption("فئة الدفع الرباعي من J8: عند 60 ألف كم يصبح الإلزامي 1,901.20 رينغت بسبب سوائل الدبل.")
        elif chosen == "Jaecoo J7":
            st.caption("فئة الدفع الرباعي من J7: عند 60 ألف كم يصبح الإلزامي 1,690.90 رينغت بسبب سوائل الدبل.")
        rows = list_maintenance_rows(_SERVICE_MODELS[chosen])
        if not rows:
            st.warning("لا توجد صفوف صيانة لهذا الطراز في قاعدة المرجع.")
        else:
            frame = pd.DataFrame([{
                "المسافة (كم)": row["mileage_km"],
                "القطع والسوائل": row["replaced_parts"],
                "الإلزامي (رينغت)": row["compulsory_amount"],
                "الإجمالي المنشور (رينغت)": row["published_total"],
            } for row in rows])
            st.dataframe(frame, use_container_width=True, hide_index=True)
            st.caption(
                f"مجموع الإلزامي حتى 100 ألف كم: {sum(row['compulsory_amount'] for row in rows):,.2f} رينغت. "
                f"مجموع الإجمالي المنشور: {sum(row['published_total'] for row in rows):,.2f} رينغت. "
                f"المصدر: {rows[0]['source_name']}"
            )
    with parts_tab:
        part_choice = st.selectbox(
            "الطراز",
            list(_PART_MODELS),
            key="parts_model_choice",
        )
        st.caption(
            "أرقام القطع وأسعار التجزئة المنشورة بالجنيه من Jaecoo Parts Direct. "
            "هذه ليست أسعار وكيل السعودية ولا تشمل ضريبة القيمة المضافة السعودية."
        )
        if part_choice == "Jaecoo J8":
            st.info("لم يُنشر في هذا المصدر رقم قطعة مستقل لـ J8. الجدول يعرض قطع J7 الموثقة كمرجع استهلاك.")
        elif part_choice == "Jaecoo J8 SHS":
            st.info("قطع الهايبرد المعروضة هي أرقام Jaecoo J7 PHEV المنشورة، وليست قائمة وكيل J8 SHS.")
        part_rows = list_spare_parts(_PART_MODELS[part_choice])
        if not part_rows:
            st.warning("لا توجد قطع مسجلة لهذا الطراز.")
        else:
            part_frame = pd.DataFrame([{
                "القطعة": row["part_name"],
                "رقم القطعة": row["part_number"],
                "التصنيف": row["part_group"],
                "السعر المنشور (جنيه)": row["price"],
            } for row in part_rows])
            st.dataframe(part_frame, use_container_width=True, hide_index=True)


_render_service_reference()

df = _load_cards_dataframe()

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
    col_j8, col_j8_shs, col_j7, col_j5, col_all = st.columns(5)
    with col_j8:
        _render_stat_card("بطاقات Jaecoo J8", _model_count(frame, "Jaecoo J8"), "🚙")
    with col_j8_shs:
        _render_stat_card("بطاقات Jaecoo J8 SHS", _model_count(frame, "Jaecoo J8 SHS"), "⚡")
    with col_j7:
        _render_stat_card("بطاقات Jaecoo J7", _model_count(frame, "Jaecoo J7"), "🚘")
    with col_j5:
        _render_stat_card("بطاقات Jaecoo J5", _model_count(frame, "Jaecoo J5"), "🚗")
    with col_all:
        _render_stat_card("جميع الطرازات / عام", _model_count(frame, "جميع الطرازات / عام"), "🌐")
    shs_count = _model_count(frame, "Jaecoo J8 SHS")
    if frame.empty or "car_model" not in frame.columns or shs_count == 0:
        st.caption(f"إجمالي البطاقات الآن {total_cards}. بطاقات Jaecoo J8 SHS الظاهرة: 0.")
    else:
        shs_titles = frame.loc[
            frame["car_model"].fillna("").str.strip().str.upper().eq("JAECOO J8 SHS"),
            "title",
        ].fillna("").head(3)
        latest = "، ".join(title for title in shs_titles if str(title).strip())
        st.caption(f"إجمالي البطاقات الآن {total_cards}. بطاقات Jaecoo J8 SHS الظاهرة: {shs_count}. أحدثها: {latest}")

@st.fragment(run_every=timedelta(seconds=4))
def _live_archive_metrics():
    """يحدّث عدادات البطاقات كل 4 ثوانٍ أثناء السحب باستعلام خفيف وسريع."""
    with _SYNC_GUARD:
        running = bool(_SYNC_STATE["running"])
    if running:
        live = get_db_connection()
        try:
            frame = pd.read_sql_query(
                "SELECT car_model, is_verified_solution, title FROM knowledge_cards ORDER BY id DESC",
                live,
            )
        finally:
            live.close()
    else:
        frame = df
    _render_metrics(frame)


_live_archive_metrics()

if st.session_state.get("sync_notice"):
    st.success(st.session_state.pop("sync_notice"))
if st.session_state.get("sync_error"):
    st.error(st.session_state.get("sync_error"))

if is_admin:
    with st.expander("🎙️📷 مختبر التحليل الذكي", expanded=False):
        lab_text = st.text_area("النص المراد تحليله", key="public_lab_text", height=120)
        lab_file = st.file_uploader(
            "مرفق اختياري",
            type=["png", "jpg", "jpeg", "mp3", "wav", "ogg", "m4a", "mp4"],
            key="public_lab_file",
        )

    render_telegram_login(cfg, expanded=_telegram_login_needed(cfg))

    limit_val = st.slider(
        "عدد الرسائل في السحبة الواحدة",
        min_value=500,
        max_value=10000,
        value=2000,
        step=500,
        key="public_pull_limit_forward",
        help="يسحب حتى 10000 رسالة في الضغطة الواحدة على دفعات من 100.",
    )
    st.caption("زر (تحديث قاعدة الأرشيف) يبدأ من 1/1/2026 أو يكمل من آخر مؤشر. زر (من البداية) يسحب القروب كاملاً من الرسالة رقم 1.")
    opt_col1, opt_col2 = st.columns(2)
    with opt_col1:
        skip_media = st.checkbox(
            "⚡ السحب الصاروخي السريع (تخطي تحميل الوسائط مع حفظ تصنيفها)",
            value=True,
            key="turbo_skip_media",
        )
    with opt_col2:
        use_batch_ai = st.checkbox(
            "🧠 التحليل المجمّع (دمج كل 8 نقاشات في طلب واحد لتوفير حصة Groq)",
            value=False,
            key="batch_ai_toggle",
        )
    if st.button("🔄 إعادة الضبط للبدء من 1/1/2026", key="reset_sync_2026"):
        reset_sync_checkpoint()
        with _SYNC_GUARD:
            _SYNC_STATE["running"] = False
            _SYNC_STATE["done_at"] = 0
        st.session_state.pop("sync_error", None)
        st.success("تم تصفير المؤشر وفك قفل الجلسة. ضغطة (تحديث قاعدة الأرشيف) القادمة ستبدأ مباشرة من 1/1/2026.")
    with st.expander("🛠️ عرض تفاصيل وسجلات السحب الحي (اضغط للإخفاء/الإظهار)", expanded=False):
        @st.fragment(run_every=timedelta(seconds=4))
        def _live_sync_log():
            with _SYNC_GUARD:
                running = bool(_SYNC_STATE["running"])
                lines = list(_SYNC_STATE["lines"])
                fraction = float(_SYNC_STATE["fraction"] or 0.0)
                message = str(_SYNC_STATE["message"] or "")
                result = _SYNC_STATE["result"]
                done_at = _SYNC_STATE["done_at"]
            shown = lines or list(st.session_state.get("sync_logs") or [])
            if shown:
                st.code("\n".join(shown[-_SYNC_LINE_LIMIT:]), language="text")
            if running:
                st.progress(min(1.0, max(0.0, fraction)), text=message or "جاري السحب...")
                return
            if not result or done_at == st.session_state.get("seen_manual_sync_done"):
                return
            st.session_state["seen_manual_sync_done"] = done_at
            st.session_state["sync_logs"] = lines[-_SYNC_LINE_LIMIT:]
            fresh = get_db_connection()
            try:
                fresh_df = pd.read_sql_query("SELECT car_model FROM knowledge_cards", fresh)
            finally:
                fresh.close()
            total_cards = len(fresh_df)
            if result.get("ok"):
                st.session_state.pop("sync_error", None)
                stopped_note = "تم إيقاف السحب والاكتفاء بالمحفوظ. " if result.get("stopped") else ""
                hint = f"{result.get('hint')} | " if result.get("hint") else ""
                st.session_state["sync_notice"] = (
                    f"{hint}{stopped_note}"
                    f"اسم القروب: ({result.get('group') or 'التلجرام'}) | "
                    f"فُحصت: {result.get('scanned', 0)} رسالة | "
                    f"سلاسل النقاش: {result.get('threads', 0)} | "
                    f"استُبعدت كترحيب عابر: {result.get('skipped', 0)} | "
                    f"حُفظت/حُدثت: {result.get('saved', 0)} بطاقة. "
                    f"الإجمالي الآن {total_cards}. بطاقات Jaecoo J8 SHS: {_model_count(fresh_df, 'Jaecoo J8 SHS')}."
                )
            else:
                st.session_state["sync_error"] = str(result.get("error") or "توقف السحب بسبب خطأ غير متوقع.")
            try:
                st.rerun(scope="app")
            except TypeError:
                st.rerun()

        _live_sync_log()

    def _launch_pull(force_from_start: bool) -> None:
        opening = (
            "جاري سحب القروب من الرسالة رقم 1..."
            if force_from_start
            else (
                "جاري الاتصال بملف الجلسة المحلي..."
                if os.path.exists(PERSONAL_SESSION_FILE)
                else "جاري الاتصال بجلسة الحساب..."
            )
        )
        start_pull = False
        with _SYNC_GUARD:
            if not _SYNC_STATE["running"]:
                start_pull = True
                _SYNC_STATE["running"] = True
                _SYNC_STATE["result"] = None
                _SYNC_STATE["lines"] = [opening]
                _SYNC_STATE["fraction"] = 0.08
                _SYNC_STATE["message"] = opening
        if not start_pull:
            st.warning("توجد عملية سحب جارية. انتظر اكتمالها أو أوقفها.")
            return
        st.session_state.pop("sync_error", None)
        if force_from_start:
            reset_sync_checkpoint()
        clear_stop_sync()
        publish_groq_env(cfg)
        explicit_key = os.environ.get("GROQ_API_KEY") or ""
        chosen_limit = int(limit_val)
        chosen_skip = bool(skip_media)
        chosen_force = bool(force_from_start)
        chosen_batch = bool(use_batch_ai)

        def _pull_worker() -> None:
            try:
                result = run_sync(
                    chosen_limit,
                    on_progress=_record_sync_progress,
                    api_key=explicit_key,
                    skip_media=chosen_skip,
                    force_from_start=chosen_force,
                    use_batch_ai=chosen_batch,
                )
            except Exception as exc:
                result = {"ok": False, "saved": 0, "error": describe_groq_error(exc), "logs": []}
            with _SYNC_GUARD:
                _SYNC_STATE["result"] = result
                _SYNC_STATE["done_at"] = time.time()
                _SYNC_STATE["running"] = False

        threading.Thread(target=_pull_worker, daemon=True).start()

    if st.button("🚀 سحب القروب كاملاً من البداية الآن (من رسالة 1)", use_container_width=True, key="force_pull_from_start"):
        _launch_pull(True)
    pull_col, stop_col, analyze_col, sample_col = st.columns(4)
    with pull_col:
        if st.button("🔄 تحديث قاعدة الأرشيف بالذكاء الاصطناعي", use_container_width=True, key="public_pull"):
            _launch_pull(False)
    with stop_col:
        if st.button("⏹️ إيقاف السحب والاكتفاء بما حُفظ", use_container_width=True, key="stop_pull"):
            request_stop_sync()
            st.warning("سيتوقف السحب بعد المجموعة الحالية، وتُحفظ نقطة آخر نقاش اكتمل.")
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
                        group_hint = str(cfg.get("tg_group") or "")
                        source_text, media_type, media_b64 = _prepare_lab_source(lab_text, lab_file, groq_key)
                        card = analyze_thread_with_ai(
                            source_text or "حلل المرفق",
                            groq_key,
                            group_name=group_hint,
                        )
                        msg_id = -int(time.time() * 1000)
                        saved = save_card(
                            card,
                            msg_id,
                            "",
                            "مختبر التحليل",
                            source_text or "مرفق وسائط",
                            media_type,
                            media_b64,
                            group_name=group_hint,
                        )
                        if not saved:
                            st.warning("لم تُحفظ البطاقة لأن العنوان غير عربي أو المحتوى غير مفيد.")
                        else:
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
            "المحرك والتيربو والمنظومة الهجين SHS",
            "القير والدفع الرباعي AWD",
            "الكهرباء والبطارية والحساسات",
            "الشاشة والنظام الصوتي والربط",
            "التكييف والتبريد",
            "العضلات والفرامل والتعليق",
            "الهيكل الخارجي والمقصورة",
            "الإكسسوارات ومنتجات AliExpress",
            "تقييم العملية والمميزات والعيوب",
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
                msg_id = -int(time.time() * 1000)
                manual_b64 = _image_preview(m_image.getvalue()) if m_image is not None else ""
                manual_type = "image" if manual_b64 else "text"
                if save_card(card_manual, msg_id, "", "إضافة يدوية", m_prob, manual_type, manual_b64):
                    st.session_state["sync_notice"] = "تمت إضافة البطاقة!"
                    st.rerun()
                else:
                    st.warning("لم تُحفظ البطاقة. استخدم عنواناً عربياً واملأ الوصف والخلاصة.")
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
        help="يشمل Jaecoo J8 SHS للسيارات الهجينة: SHS، هايبرد، هجين، الشاحن، والبطارية الكبيرة.",
    )

with st.expander("🤖 استشارة المهندس الذكي", expanded=False):
    user_q = st.text_input("اسأل المهندس الذكي عن أي عطل أو قطعة غيار:", key="consult_question")
    if is_admin:
        consult_col, pulse_col = st.columns(2)
    else:
        consult_col = st.container()
        pulse_col = None
    with consult_col:
        if st.button("بحث واستشارة فورية 💡", use_container_width=True, key="consult_search"):
            ready_at = float(st.session_state.get("consult_ready_at") or 0)
            remaining = int(ready_at - time.time())
            if remaining > 0:
                st.info(f"التهدئة مستمرة. يمكن طرح الاستشارة التالية بعد {remaining} ثانية.")
            elif not cfg.get("groq_api_key"):
                st.warning("أدخل مفتاح Groq API في الإعدادات أولاً.")
            elif user_q:
                st.session_state["consult_ready_at"] = time.time() + CONSULT_COOLDOWN_SECONDS
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
    if is_admin and pulse_col is not None:
        with pulse_col:
            if st.button("⚡ توليد الموجز الذكي لنبض القروب", use_container_width=True, key="consult_pulse"):
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
if model_filter != "الكل" and not filtered_df.empty and "car_model" in filtered_df.columns:
    filtered_df = filtered_df[
        filtered_df["car_model"].fillna("").str.strip().str.upper() == model_filter.strip().upper()
    ]

if search_q.strip() and not filtered_df.empty:
    needle = _normalize_arabic(search_q.strip())
    mask = False
    for column in ("title", "problem_or_topic", "solution_or_summary", "part_numbers_or_specs", "keywords"):
        if column not in filtered_df.columns:
            continue
        folded = filtered_df[column].fillna("").map(_normalize_arabic)
        mask = mask | folded.str.contains(needle, case=False, regex=False, na=False)
    filtered_df = filtered_df[mask]

categories = ["الكل", "الأعطال والتشخيص", "الإصلاحات والحلول المؤكدة", "بنك القطع البديلة والمطابقات", "الأنظمة الذكية والشاشات", "الصيانة الدورية والوكالات", "الإكسسوارات والتجهيزات", "تجارب القيادة والأداء الفعلي", "نبض القروب والدردشة العامة"]
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
verified_only = st.checkbox("✅ الحلول المؤكدة فقط", key="verified_only_filter")
if verified_only and not filtered_df.empty and "is_verified_solution" in filtered_df.columns:
    filtered_df = filtered_df[filtered_df["is_verified_solution"].fillna(0).astype(int).eq(1)]

if filtered_df.empty:
    if is_admin:
        st.info("لا توجد بطاقات مطابقة حالياً. استخدم زر سحب رسائل القروب أو بدء التحليل في أعلى الصفحة.")
    else:
        st.info("لا توجد بطاقات مطابقة حالياً.")
else:
    page_count = max(1, (len(filtered_df) + CARD_PAGE_SIZE - 1) // CARD_PAGE_SIZE)
    filter_signature = (search_q.strip(), model_filter, selected_cat, selected_media, verified_only)
    if st.session_state.get("card_filter_sig") != filter_signature:
        st.session_state["card_filter_sig"] = filter_signature
        st.session_state["card_page"] = 1
    page = min(max(int(st.session_state.get("card_page") or 1), 1), page_count)
    st.session_state["card_page"] = page
    prev_col, page_col, next_col = st.columns([1, 2, 1])
    with prev_col:
        if st.button("الصفحة السابقة", use_container_width=True, disabled=page <= 1, key="card_page_prev"):
            st.session_state["card_page"] = page - 1
            st.rerun()
    with page_col:
        st.caption(f"صفحة {page} من {page_count} — {len(filtered_df)} بطاقة")
    with next_col:
        if st.button("الصفحة التالية", use_container_width=True, disabled=page >= page_count, key="card_page_next"):
            st.session_state["card_page"] = page + 1
            st.rerun()
    page_frame = filtered_df.iloc[(page - 1) * CARD_PAGE_SIZE:page * CARD_PAGE_SIZE]
    page_media_map = get_cards_media_map(page_frame["id"].tolist())
    for _, row in page_frame.iterrows():
        card_id = int(row["id"])
        title_safe = html.escape(_card_text(row.get("title")))
        prob_safe = format_clickable_links(_card_text(row.get("problem_or_topic")))
        sol_safe = format_clickable_links(_card_text(row.get("solution_or_summary")))
        parts_raw = _card_text(row.get("part_numbers_or_specs"))
        parts_safe = format_clickable_links(parts_raw)
        car_safe = html.escape(_card_text(row.get("car_model")))
        sub_safe = html.escape(_card_text(row.get("sub_system")))
        cat_safe = html.escape(_card_text(row.get("category")))
        media_key = str(row.get("media_type") or "text").strip().lower()
        badge = MEDIA_BADGES.get(media_key, MEDIA_BADGES["text"])
        media_html = (
            f'<span class="k-tag" style="color:{badge["color"]};border-color:{badge["color"]}">'
            f'{badge["icon"]} {html.escape(badge["label"])}</span>'
        )
        is_verified = int(row.get("is_verified_solution") or 0) == 1
        verified_html = (
            '<span class="k-tag" style="color:#34d399;border-color:#10b981;background:rgba(16,185,129,0.14);">'
            '✅ حل مؤكد</span>'
            if is_verified
            else ""
        )
        thread_url_raw = _card_text(row.get("thread_url"))
        thread_link_html = ""
        if thread_url_raw.startswith(("https://t.me/", "http://t.me/")):
            safe_thread_url = html.escape(thread_url_raw, quote=True)
            thread_link_html = (
                f'<a href="{safe_thread_url}" target="_blank" class="k-tag" '
                f'style="color:#bae6fd;border-color:rgba(56,189,248,0.45);text-decoration:none;">'
                f'🔗 النقاش الأصلي</a>'
            )
        tags_joined = "".join(
            tag for tag in (
                f'<span class="k-tag">🚙 {car_safe}</span>',
                f'<span class="k-tag">⚙️ {sub_safe}</span>',
                f'<span class="k-tag">📂 {cat_safe}</span>',
                media_html,
                verified_html,
                thread_link_html,
            ) if tag
        )
        parts_html = (
            f'<div class="k-section">🔧 <b>القطع والمواصفات الفنية:</b> {parts_safe}</div>'
            if parts_raw and parts_raw != "غير محدد"
            else ""
        )
        card_html = (
            f'<div class="k-card">'
            f'<div class="k-tags">{tags_joined}</div>'
            f'<div class="k-title">{title_safe.replace(chr(10), " ")}</div>'
            f'<div class="k-section">🔹 <b>الوصف / المشكلة:</b> {prob_safe}</div>'
            f'<div class="k-section">⚡ <b>الخلاصة / الحل:</b> {sol_safe}</div>'
            f'{parts_html}'
            f'</div>'
        )
        st.markdown(card_html, unsafe_allow_html=True)
        preview_bytes = _decode_card_image(page_media_map.get(card_id, ""))
        if preview_bytes:
            with st.expander("🖼️ عرض الصورة المرفقة مع البطاقة"):
                enlarge = st.toggle("تكبير الصورة", key=f"zoom_card_{card_id}")
                st.image(preview_bytes, use_container_width=enlarge)
        if is_admin:
            if st.button(f"🗑️ حذف البطاقة #{card_id}", key=f"delete_card_{card_id}"):
                st.session_state["pending_delete_id"] = card_id
            if st.session_state.get("pending_delete_id") == card_id:
                st.warning(f"تأكيد حذف البطاقة #{card_id}؟")
                if st.button("تأكيد الحذف", key=f"confirm_delete_{card_id}"):
                    delete_knowledge_card(card_id)
                    st.session_state["pending_delete_id"] = None
                    st.rerun()

if not is_admin:
    with st.expander("🔐 دخول المشرف"):
        admin_pin = st.text_input("رمز المشرف", type="password", key="admin_pin_input")
        if st.button("دخول", key="admin_pin_login"):
            if _pin_matches(admin_pin, cfg):
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("رمز المشرف غير صحيح.")