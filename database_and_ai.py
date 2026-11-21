import base64
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field
from groq import Groq

DB_NAME = "jaecoo_knowledge.db"
CONFIG_FILE = "config.json"

# ==========================================
# 1. إدارة الإعدادات الدائمة (Config Manager)
# ==========================================
def load_config() -> dict:
    default_cfg = {
        "groq_api_key": "",
        "tg_api_id": "",
        "tg_api_hash": "",
        "tg_group": "",
        "bot_token": "",
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_cfg.update(data)
        except Exception:
            pass
    return default_cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ==========================================
# 2. القالب المهيكل للبطاقة الفنية (لطرازات جايكو)
# ==========================================
class KnowledgeCardSchema(BaseModel):
    is_worth_archiving: bool = Field(
        description="True لأي معلومة أو استفسار أو رد فني أو وسائط مفيدة. False للتحيات والدردشة الفارغة فقط."
    )
    category: Literal[
        "الأعطال والتشخيص",
        "الإصلاحات والحلول المؤكدة",
        "بنك القطع البديلة والمطابقات",
        "الأنظمة الذكية والشاشات",
        "الصيانة الدورية والوكالات",
        "الإكسسوارات والتجهيزات",
        "تجارب القيادة والأداء الفعلي",
        "نبض القروب والدردشة العامة",
    ] = Field(description="القسم الأنسب من الأقسام الثمانية")

    car_model: Literal["Jaecoo J8", "Jaecoo J8 SHS", "Jaecoo J5", "Jaecoo J7", "جميع الطرازات / عام"] = Field(
        description="طراز السيارة"
    )
    sub_system: Literal[
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
    ] = Field(description="النظام الفرعي. معلومات الوكيل والصيانة الدورية تستخدم أول خيارين.")

    title: str = Field(description="عنوان بالعربية الفصحى حصراً، من 5 إلى 10 كلمات. ممنوع الإنجليزية وممنوع القيمة nan.")
    problem_or_topic: str = Field(description="الموضوع أو الاستفسار أو المشكلة بالعربية الفصحى. في بطاقات الوكيل اذكر نوع الاستفسار مثل تكلفة الصيانة أو موقع الفرع.")
    solution_or_summary: str = Field(description="التفاصيل الكاملة أو اسم الوكيل والمدينة والسعر أو الحل الفني، بالعربية الفصحى. ممنوع nan.")
    part_numbers_or_specs: Optional[str] = Field(
        default="غير محدد",
        description="الزيوت والفلاتر المشمولة أو اسم الفرع أو المركز، أو غير محدد. ممنوع nan.",
    )
    is_verified_solution: bool = Field(
        description="True إذا تضمن النقاش حلاً مجرباً ومؤكداً"
    )
    keywords: str = Field(description="كلمات مفتاحية عربية مفصولة بفاصلة")


COMPACT_SCHEMA_GUIDE = """{
  "is_worth_archiving": true,
  "category": "أحد الأقسام: الأعطال والتشخيص | الإصلاحات والحلول المؤكدة | بنك القطع البديلة والمطابقات | الأنظمة الذكية والشاشات | الصيانة الدورية والوكالات | الإكسسوارات والتجهيزات | تجارب القيادة والأداء الفعلي | نبض القروب والدردشة العامة",
  "car_model": "أحد الطرازات: Jaecoo J8 | Jaecoo J8 SHS | Jaecoo J7 | Jaecoo J5 | جميع الطرازات / عام",
  "sub_system": "أحد الأنظمة: الوكيل والمراكز المعتمدة | جدول وتكاليف الصيانة الدورية | المحرك والتيربو والمنظومة الهجين SHS | القير والدفع الرباعي AWD | الكهرباء والبطارية والحساسات | الشاشة والنظام الصوتي والربط | التكييف والتبريد | العضلات والفرامل والتعليق | الهيكل الخارجي والمقصورة | الإكسسوارات ومنتجات AliExpress | تقييم العملية والمميزات والعيوب | عام",
  "title": "عنوان عربي فصيح من 5 إلى 10 كلمات",
  "problem_or_topic": "وصف المشكلة أو الاستفسار أو الموضوع بالعربية",
  "solution_or_summary": "خلاصة الحلول وتجارب الأعضاء أو ملخص الاستفسار بالعربية",
  "part_numbers_or_specs": "أرقام القطع أو اللزوجة أو السعر أو الرابط، أو غير محدد",
  "is_verified_solution": false,
  "keywords": "كلمات مفتاحية عربية مفصولة بفاصلة"
}"""


DEALER_MAINTENANCE_KEYWORDS = [
    "وكيل", "الوكيل", "وكالة", "الوكالة", "صيانة دورية", "الصيانة الدورية",
    "مركز معتمد", "مراكز معتمدة", "الوكيل المعتمد", "ضمان", "الضمان",
    "موعد صيانة", "فرع", "فروع", "جدول الصيانة", "تكلفة الصيانة",
    "صيانة 5000", "صيانة 10000", "صيانة ١٠٠٠٠", "صيانة ٥٠٠٠", "تغيير زيت القير",
]


def is_dealer_or_maintenance_msg(text: str) -> bool:
    """يفحص هل الرسالة تتحدث عن الوكيل أو الصيانة الدورية."""
    if not text:
        return False
    return any(keyword in text for keyword in DEALER_MAINTENANCE_KEYWORDS)


def extract_urls(text: str) -> list:
    if not text:
        return []
    return re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', text)


def is_product_link_text(text: str) -> bool:
    """روابط علي إكسبريس ومنتجات الإكسسوارات تُحفظ حتى لو كان النص قصيراً."""
    raw = text or ""
    lower = raw.lower()
    return (
        "aliexpress" in lower
        or "علي اكسبرس" in raw
        or "علي إكسبريس" in raw
    )


def is_owner_experience_msg(text: str) -> bool:
    if not text:
        return False
    markers = (
        "مميزات", "عيوب", "اعتمادية", "العزل", "صرفية", "رساوة",
        "هل السيارة عملية", "تجربة السيارة", "بعد ممشى", "عملية السيارة",
    )
    return any(marker in text for marker in markers)


SYSTEM_PROMPT = """
أنت خبير فني ومستشار لملاك سيارات جايكو (Jaecoo).
استخرج بطاقة معرفية بالعربية الفصحى في أربعة مجالات:
1. منتجات وإكسسوارات علي إكسبريس والتجهيزات: أي رابط aliexpress أو صورة إكسسوار -> category = الإكسسوارات والتجهيزات، sub_system = الإكسسوارات ومنتجات AliExpress، مع إبقاء الرابط في الحل أو القطع.
2. تجارب القيادة والعملية والمميزات والعيوب والعزل والصرفية: category = تجارب القيادة والأداء الفعلي، sub_system = تقييم العملية والمميزات والعيوب.
3. الوكيل والصيانة الدورية والضمان وتكاليف الصيانات: category = الصيانة الدورية والوكالات.
4. الأعطال والحلول والشاشات والقطع البديلة: المشكلة في problem_or_topic، والحل أو خلاصة الردود في solution_or_summary (وإن لم يرد حل بعد فلخّص الاستفسار ولا ترفض البطاقة).
شروط صارمة:
- العربية الفصحى فقط مع السماح بروابط الإنترنت وأرقام القطع والمصطلحات مثل AGM وCarPlay وTbox وOBD وAWD وSHS. ممنوع nan.
- التحيات والدردشة الفارغة بلا معلومة ولا استفسار تعني is_worth_archiving = false.
- Jaecoo J7 مع هايبرد أو PHEV تبقى Jaecoo J7 ما لم يُذكر J8 أو SHS.
- Jaecoo J8 SHS عند ذكر SHS، أو هايبرد مع J8، أو هايبرد دون تحديد J7 أو J5.
"""

# ==========================================
# 3. إدارة قاعدة البيانات (SQLite + WAL)
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_msg_id INTEGER UNIQUE,
            thread_url TEXT,
            category TEXT,
            car_model TEXT,
            sub_system TEXT,
            title TEXT,
            problem_or_topic TEXT,
            solution_or_summary TEXT,
            part_numbers_or_specs TEXT,
            is_verified_solution INTEGER,
            keywords TEXT,
            author_name TEXT,
            raw_thread_text TEXT,
            created_at TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE knowledge_cards ADD COLUMN media_type TEXT DEFAULT 'text'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE knowledge_cards ADD COLUMN media_b64 TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            group_key TEXT PRIMARY KEY,
            last_msg_id INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    _ensure_service_tables(conn)
    _seed_service_reference(conn)
    conn.commit()
    conn.close()


SERVICE_SOURCE_KEY = "oj-my-2026"
SERVICE_SOURCE_NAME = "جدول OMODA | JAECOO الرسمي المنشور لعام 2026"
SERVICE_SOURCE_URL = "https://paultan.org/2026/06/26/omoda-jaecoo-service-cost-malaysia/"
PARTS_SOURCE_KEY = "jaecoo-parts-direct"
PARTS_SOURCE_NAME = "Jaecoo Parts Direct"
PARTS_SOURCE_URL = "https://jaecoopartsdirect.co.uk/"


def _ensure_service_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_model TEXT NOT NULL,
            mileage_km INTEGER NOT NULL,
            replaced_parts TEXT NOT NULL,
            compulsory_amount REAL NOT NULL,
            published_total REAL NOT NULL,
            currency TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            UNIQUE(car_model, mileage_km, source_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spare_part_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_model TEXT NOT NULL,
            part_name TEXT NOT NULL,
            part_number TEXT NOT NULL,
            part_group TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            UNIQUE(car_model, part_number, source_key)
        )
    """)


def _service_parts(mileage_km: int, hybrid: bool) -> str:
    parts = ["زيت المحرك", "فلتر الزيت", "فحص دوري"]
    if mileage_km in {30000, 60000, 90000}:
        parts.append("بواجي في جدول البنزين كل 30 ألف كم")
    if mileage_km in {40000, 80000}:
        parts.append("سائل الفرامل")
    if hybrid and mileage_km in {40000, 80000}:
        parts.append("زيت القير")
    elif mileage_km == 60000:
        parts.append("زيت القير")
    if mileage_km in {20000, 40000, 60000, 80000, 100000}:
        parts.append("عند ارتفاع الإجمالي: فلتر المكيف وفلتر الهواء والمساحات")
    return "، ".join(parts)


def _seed_service_reference(conn) -> None:
    """يعيد كتابة الجداول المرجعية المنشورة دون المساس ببطاقات الأرشيف."""
    conn.execute("DELETE FROM maintenance_schedule WHERE source_key = ?", (SERVICE_SOURCE_KEY,))
    conn.execute("DELETE FROM spare_part_prices WHERE source_key = ?", (PARTS_SOURCE_KEY,))
    schedules = {
        "Jaecoo J5": (False, [
            (10000, 405.50, 405.50), (20000, 405.50, 835.20), (30000, 622.90, 622.90),
            (40000, 555.50, 1127.00), (50000, 405.50, 405.50), (60000, 995.90, 1425.60),
            (70000, 405.50, 405.50), (80000, 555.50, 1127.00), (90000, 622.90, 622.90),
            (100000, 405.50, 835.20),
        ]),
        "Jaecoo J7": (False, [
            (10000, 405.50, 405.50), (20000, 405.50, 865.10), (30000, 622.90, 622.90),
            (40000, 505.50, 965.10), (50000, 405.50, 689.10), (60000, 1290.90, 1750.50),
            (70000, 405.50, 405.50), (80000, 505.50, 965.10), (90000, 622.90, 622.90),
            (100000, 405.50, 1148.70),
        ]),
        "Jaecoo J7 PHEV": (True, [
            (10000, 406.20, 406.20), (20000, 406.20, 901.90), (30000, 764.60, 764.60),
            (40000, 959.80, 2022.70), (50000, 406.20, 406.20), (60000, 764.60, 1260.30),
            (70000, 406.20, 406.20), (80000, 959.80, 2022.70), (90000, 764.60, 764.60),
            (100000, 406.20, 901.90),
        ]),
        "Jaecoo J8": (False, [
            (10000, 385.50, 385.50), (20000, 385.50, 830.90), (30000, 669.10, 669.10),
            (40000, 513.50, 958.90), (50000, 385.50, 669.10), (60000, 1601.20, 2046.60),
            (70000, 385.50, 385.50), (80000, 513.50, 958.90), (90000, 669.10, 669.10),
            (100000, 385.50, 1114.50),
        ]),
    }
    rows = []
    for model, (hybrid, visits) in schedules.items():
        for mileage_km, compulsory, published in visits:
            rows.append((
                model, mileage_km, _service_parts(mileage_km, hybrid),
                compulsory, published, "MYR",
                SERVICE_SOURCE_KEY, SERVICE_SOURCE_NAME, SERVICE_SOURCE_URL,
            ))
    conn.executemany(
        """
        INSERT INTO maintenance_schedule (
            car_model, mileage_km, replaced_parts, compulsory_amount, published_total,
            currency, source_key, source_name, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    parts = [
        ("Jaecoo J5", "فلتر الزيت", "110900104AA", "استهلاكية", 13.25),
        ("Jaecoo J5", "بواجي", "111200100AA", "استهلاكية", 15.70),
        ("Jaecoo J5", "فحمات خلفية", "T13J-6GN3502080AB", "استهلاكية", 158.70),
        ("Jaecoo J5", "فلتر الهواء", "151000079AA", "استهلاكية", 11.42),
        ("Jaecoo J7", "فلتر الزيت", "480-1012010", "استهلاكية", 19.39),
        ("Jaecoo J7", "فلتر الهواء", "151000079AA", "استهلاكية", 11.42),
        ("Jaecoo J7", "فحمات أمامية", "T1EJ-3501080AB", "استهلاكية", 198.82),
        ("Jaecoo J7", "فحمات خلفية", "T22-3502080", "استهلاكية", 170.44),
        ("Jaecoo J7", "قرص فرامل أمامي", "204004261AA", "استهلاكية", 114.77),
        ("Jaecoo J7", "قرص فرامل خلفي", "204004266AA", "استهلاكية", 85.68),
        ("Jaecoo J7 PHEV", "فلتر الهواء", "151000256AA", "استهلاكية", 61.18),
        ("Jaecoo J7 PHEV", "كاليبر خلفي يمين", "204004265AA", "هيكل ومكابح", 317.36),
        ("Jaecoo J7 PHEV", "كاليبر خلفي يسار", "204004264AA", "هيكل ومكابح", 317.36),
        ("Jaecoo J5", "شبك الصدام الأمامي لطراز J5 BEV", "602008034AA", "هيكل", 61.55),
    ]
    conn.executemany(
        """
        INSERT INTO spare_part_prices (
            car_model, part_name, part_number, part_group, price, currency,
            source_key, source_name, source_url
        ) VALUES (?, ?, ?, ?, ?, 'GBP', ?, ?, ?)
        """,
        [(*item, PARTS_SOURCE_KEY, PARTS_SOURCE_NAME, PARTS_SOURCE_URL) for item in parts],
    )


def list_maintenance_rows(car_model: str) -> list[dict]:
    conn = get_db_connection()
    try:
        found = conn.execute(
            """
            SELECT mileage_km, replaced_parts, compulsory_amount, published_total, currency, source_name, source_url
            FROM maintenance_schedule
            WHERE car_model = ? AND source_key = ?
            ORDER BY mileage_km
            """,
            (car_model, SERVICE_SOURCE_KEY),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "mileage_km": row[0],
            "replaced_parts": row[1],
            "compulsory_amount": row[2],
            "published_total": row[3],
            "currency": row[4],
            "source_name": row[5],
            "source_url": row[6],
        }
        for row in found
    ]


def list_spare_parts(car_model: str) -> list[dict]:
    conn = get_db_connection()
    try:
        found = conn.execute(
            """
            SELECT car_model, part_name, part_number, part_group, price, currency, source_name, source_url
            FROM spare_part_prices
            WHERE car_model = ? AND source_key = ?
            ORDER BY part_group, part_name
            """,
            (car_model, PARTS_SOURCE_KEY),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "car_model": row[0],
            "part_name": row[1],
            "part_number": row[2],
            "part_group": row[3],
            "price": row[4],
            "currency": row[5],
            "source_name": row[6],
            "source_url": row[7],
        }
        for row in found
    ]


def get_cards_media_map(card_ids: list[int]) -> dict[int, str]:
    """يجلب الصور المضغوطة لصفحة البطاقات المعروضة فقط لتخفيف استهلاك الذاكرة."""
    clean_ids = [int(cid) for cid in (card_ids or []) if cid is not None]
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            f"SELECT id, media_b64 FROM knowledge_cards "
            f"WHERE id IN ({placeholders}) AND media_b64 IS NOT NULL AND media_b64 != ''",
            clean_ids,
        ).fetchall()
    finally:
        conn.close()
    return {int(row[0]): str(row[1]) for row in rows}


def get_app_setting(key: str) -> str:
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


def set_app_setting(key: str, value: str) -> None:
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO app_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def load_telegram_session() -> str:
    return get_app_setting("telegram_string_session")


def save_telegram_session(session_text: str) -> None:
    """يحفظ String Session الجديدة محلياً ويعلّم الجلسة صالحة."""
    set_app_setting("telegram_string_session", session_text.strip())
    set_app_setting("telegram_session_status", "ok")
    set_app_setting("invalid_tg_session", "")
    cfg = load_config()
    cfg["tg_session"] = session_text.strip()
    save_config(cfg)


def mark_telegram_session_invalid() -> None:
    set_app_setting("telegram_session_status", "invalid")
    if str(get_app_setting("invalid_tg_session") or "").strip():
        return
    current = str(get_app_setting("telegram_string_session") or "").strip()
    if current:
        set_app_setting("invalid_tg_session", current)


def telegram_session_status() -> str:
    return get_app_setting("telegram_session_status")


def compress_image_to_b64(image_bytes: bytes, max_size: int = 600, quality: int = 70) -> str:
    """يصغّر الصورة إلى 600×600 وجودة JPEG 70٪ ثم يعيدها كنص Base64."""
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode in {"RGBA", "LA", "P"}:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    image.thumbnail((max_size, max_size), resample)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


_CARD_SAVE_LOCK = threading.Lock()


def save_card(
    card_data: dict,
    telegram_msg_id: int,
    thread_url: str,
    author_name: str,
    raw_text: str,
    media_type: str = "text",
    media_b64: str = "",
    group_name: str = "",
):
    """يحفظ البطاقة فوراً. الرسالة نفسها تُحدَّث في صفها، ولا تُستبدل بطاقة أخرى."""
    prepared = _hint_model_from_group(dict(card_data or {}), raw_text or "", group_name)
    card_data = merge_missing_links(prepared, raw_text or "")
    if _field_is_blank(card_data.get("problem_or_topic")) and not _field_is_blank(card_data.get("title")):
        card_data["problem_or_topic"] = card_data.get("title")
    if not card_passes_quality_gate(card_data):
        return False
    params = (
        int(telegram_msg_id),
        thread_url,
        card_data["category"],
        card_data["car_model"],
        card_data["sub_system"],
        card_data["title"],
        card_data["problem_or_topic"],
        card_data["solution_or_summary"],
        card_data.get("part_numbers_or_specs", ""),
        1 if card_data.get("is_verified_solution") else 0,
        card_data.get("keywords", ""),
        author_name,
        raw_text,
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        media_type if media_type in {"text", "image", "audio", "video"} else "text",
        media_b64 or "",
    )
    with _CARD_SAVE_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO knowledge_cards (
                    telegram_msg_id, thread_url, category, car_model, sub_system,
                    title, problem_or_topic, solution_or_summary, part_numbers_or_specs,
                    is_verified_solution, keywords, author_name, raw_thread_text, created_at,
                    media_type, media_b64
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_msg_id) DO UPDATE SET
                    thread_url = excluded.thread_url,
                    category = excluded.category,
                    car_model = excluded.car_model,
                    sub_system = excluded.sub_system,
                    title = excluded.title,
                    problem_or_topic = excluded.problem_or_topic,
                    solution_or_summary = excluded.solution_or_summary,
                    part_numbers_or_specs = excluded.part_numbers_or_specs,
                    is_verified_solution = excluded.is_verified_solution,
                    keywords = excluded.keywords,
                    author_name = excluded.author_name,
                    raw_thread_text = excluded.raw_thread_text,
                    created_at = excluded.created_at,
                    media_type = excluded.media_type,
                    media_b64 = excluded.media_b64
                """,
                params,
            )
            conn.commit()
        finally:
            conn.close()
    return True

# ==========================================
# 4. محرك Groq الذكي مع إدارة الحصص الدقيقة
# ==========================================
GROQ_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]
_GROQ_COOLDOWN: dict[tuple[str, str], float] = {}
_GROQ_LAST_ERROR: dict[tuple[str, str], Exception] = {}
_GROQ_UNAVAILABLE: set[tuple[str, str]] = set()
_GROQ_DAILY_COOLDOWN_SECONDS = 600
_GROQ_MISSING_MODEL_COOLDOWN = 3600
USEFUL_TOPIC_TERMS = (
    "جايكو", "جيكو", "j8", "j7", "j5", "shs", "هايبرد", "هجين", "phev", "مشكلة", "عطل", "صوت",
    "رجة", "نتعة", "حرارة", "بطارية", "شاشة", "تكييف", "دبل", "قير", "فرامل", "فحمات", "زيت",
    "صيانة", "ضمان", "وكالة", "وكيل", "تحديث", "كفر", "إطارات", "بنزين", "صرفية", "استهلاك",
    "حساس", "لمبة", "كود", "تأمين", "قطع", "سعر", "أسعار", "كم", "كيف", "وين", "حل", "جرب", "هل",
    "تجربة", "تجارب", "استلام", "ملاحظات", "فيوز", "لزوجة", "agm", "carplay", "tbox", "obd", "awd",
)
_LATIN_TECH_TOKENS = {"agm", "carplay", "tbox", "obd", "awd", "shs", "j8", "j7", "j5", "phev"}
_NEGATION_PREFIX = r"(?:ما|لم|لن|لا|ولا|مو|مش|غير|بدون\s+ما|دون\s+أن)\s+"
_CONFIRM_WORDS_PATTERN = (
    r"انحلت|انحل|تنحل|اشتغل|اشتغلت|نجحت|نجح|زبط|زبطت|ضبط|ضبطت|"
    r"اختفت|اختفى|راحت\s+المشكلة|انتهت\s+المشكلة|تم\s+الإصلاح|تم\s+الحل|"
    r"جربته\s+ونفع|نفع\s+معي|تأكد\s+الحل"
)
SHS_MODEL_KEYWORDS = [
    "shs", "هايبرد", "هجين", "بلق ان", "بلقن", "phev", "plug-in", "plugin", "شحن كهرباء", "شحن كهربائي",
]
_MODEL_MARKERS = {
    "Jaecoo J5": ("جايكو 5", "جيكو 5", "جايكو ٥", "جيكو ٥", "جي 5", "جي ٥", "جيه 5"),
    "Jaecoo J7": ("جايكو 7", "جيكو 7", "جايكو ٧", "جيكو ٧", "جي 7", "جي ٧", "جيه 7"),
    "Jaecoo J8": ("جايكو 8", "جيكو 8", "جايكو ٨", "جيكو ٨", "جي 8", "جي ٨", "جيه 8"),
}
def has_analyzable_text(text: str) -> bool:
    """أي نص غير فارغ يذهب إلى الذكاء الاصطناعي."""
    return bool((text or "").strip())


def is_car_related_thread(text: str, media_type: str = "text") -> bool:
    """يمرر كل النصوص والصور والفيديو والصوت دون فلتر كلمات محلي."""
    if media_type in {"image", "audio", "video"}:
        return True
    return has_analyzable_text(text)


def should_analyze_thread(text: str, media_type: str = "text") -> bool:
    return is_car_related_thread(text, media_type)


def _secret_groq_values() -> list[str]:
    """يقرأ مفاتيح Groq من st.secrets فقط إذا كانت Streamlit محمّلة في العملية."""
    if "streamlit" not in sys.modules:
        return []
    try:
        import streamlit as st

        secrets = st.secrets
    except Exception:
        return []
    bags = [secrets]
    try:
        if "groq" in secrets:
            bags.append(secrets["groq"])
    except Exception:
        pass
    names = ("GROQ_API_KEY", "groq_api_key", "gemini_api_key", "GROQ_API_KEY_2", "groq_api_key_2", "GROQ_API_KEY_3", "groq_api_key_3")
    found = []
    for bag in bags:
        for name in names:
            try:
                text = str(bag.get(name, "") or "").strip()
            except Exception:
                text = ""
            if text and text not in found:
                found.append(text)
    return found


def clean_groq_key(value) -> str:
    """ينزع المسافات وعلامات التنصيص المحيطة بمفتاح Groq."""
    text = str(value or "").strip()
    quotes = "\"'`“”‘’"
    while len(text) >= 2 and text[0] in quotes and text[-1] in quotes:
        text = text[1:-1].strip()
    return text


def collect_groq_keys(primary: str = "") -> list[str]:
    """يمنح الأولوية للمفتاح الممرر صراحةً، ثم البيئة وSecrets، ويتجاهل ما لا يبدأ بـ gsk_."""
    cfg = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    keys = []

    def _add(value, *, require_prefix: bool = True) -> None:
        text = clean_groq_key(value)
        if not text or text in keys:
            return
        if require_prefix and not text.startswith("gsk_"):
            return
        keys.append(text)

    _add(primary)
    for value in (
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("GROQ_API_KEY_2"),
        os.environ.get("GROQ_API_KEY_3"),
    ):
        _add(value)
    for value in _secret_groq_values():
        _add(value, require_prefix=True)
    for value in (
        cfg.get("groq_api_key"),
        cfg.get("gemini_api_key"),
        cfg.get("groq_api_key_2"),
        cfg.get("groq_api_key_3"),
    ):
        _add(value, require_prefix=True)
    return keys


def describe_groq_error(exc: Exception) -> str:
    """سبب مختصر بلا مفتاح: الكود ثم نص السيرفر."""
    text = re.sub(r"gsk_[A-Za-z0-9_\-]+", "[key]", str(exc or ""))
    text = re.sub(r"(?i)(api[_ ]?key|authorization|bearer)\s*[:=]\s*\S+", r"\1 [key]", text)
    text = " ".join(text.split())
    code = ""
    status = getattr(exc, "status_code", None)
    if status:
        code = str(status)
    else:
        match = re.search(r"\b(400|401|403|404|413|429|500|503)\b", text)
        if match:
            code = match.group(1)
    label = code or exc.__class__.__name__
    if len(text) > 180:
        text = text[:180] + "…"
    return f"{label} / {text}" if text else label


def is_key_or_quota_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(
        token in text
        for token in (
            "401", "403", "invalid_api_key", "invalid api key",
            "429", "rate_limit", "rate limit", "quota", "حصة groq",
        )
    )


def _parse_groq_wait_seconds(error: Exception) -> int:
    """يقرأ زمن التهدئة الفعلي من رسالة Groq بدلاً من الحجز الطويل الثابت."""
    raw = str(error or "")
    low = raw.lower()
    match = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", low)
    parsed = 0
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = float(match.group(3) or 0)
        parsed = int(hours * 3600 + minutes * 60 + seconds) + 2
    is_daily = any(token in low for token in ("tokens per day", "tpd", "requests per day", "rpd", "per day"))
    if is_daily:
        return max(parsed, _GROQ_DAILY_COOLDOWN_SECONDS)
    if parsed > 0:
        return min(parsed, 900)
    return 22


def _is_model_unavailable(message: str) -> bool:
    low = (message or "").lower()
    return any(
        token in low
        for token in ("404", "model_not_found", "decommissioned", "does not exist", "not supported")
    )


def _with_json_word(messages: list) -> list:
    """Groq يرفض response_format إذا لم تظهر كلمة JSON الإنجليزية داخل الموجه."""
    blob = " ".join(str(item.get("content") or "") for item in messages if isinstance(item, dict))
    if "json" in blob.lower():
        return messages
    copied = [dict(item) for item in messages]
    if copied and copied[0].get("role") == "system":
        copied[0]["content"] = f"{copied[0].get('content') or ''}\nReturn valid JSON."
        return copied
    return [{"role": "system", "content": "Return valid JSON."}, *copied]


def _groq_create(client, model_name: str, messages: list, temperature: float, use_format: bool):
    kwargs = {"model": model_name, "messages": messages, "temperature": temperature}
    if use_format:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs)


def _groq_complete(api_key: str, messages: list, temperature: float = 0.1, json_mode: bool = False, on_switch=None):
    """يدور بين المفاتيح والنماذج، ويفصل خطأ كبر الحزمة (413) عن نفاد الحصة (429)."""
    keys = collect_groq_keys(api_key)
    if not keys:
        raise RuntimeError("401 مفتاح Groq API غير موجود")
    payload = _with_json_word(messages) if json_mode else messages
    last_error = None
    saw_payload_too_large = False
    for _wait_round in range(3):
        now = time.time()
        saw_payload_too_large = False
        for key in keys:
            client = Groq(api_key=key)
            for model_name in GROQ_MODELS:
                pair = (key, model_name)
                if _GROQ_COOLDOWN.get(pair, 0) > now:
                    continue
                use_format = json_mode and not any(tag in model_name.lower() for tag in ("qwen", "llama-4"))
                try:
                    return _groq_create(client, model_name, payload, temperature, use_format)
                except Exception as exc:
                    last_error = exc
                    _GROQ_LAST_ERROR[pair] = exc
                    message = str(exc).lower()
                    if use_format and ("400" in message or "json" in message or "response_format" in message):
                        try:
                            return _groq_create(client, model_name, payload, temperature, False)
                        except Exception as retry_exc:
                            last_error = retry_exc
                            _GROQ_LAST_ERROR[pair] = retry_exc
                            message = str(retry_exc).lower()
                    if _is_model_unavailable(message):
                        _GROQ_UNAVAILABLE.add(pair)
                        _GROQ_COOLDOWN[pair] = time.time() + _GROQ_MISSING_MODEL_COOLDOWN
                        continue
                    if "413" in message or "request too large" in message or "context_length" in message:
                        saw_payload_too_large = True
                        continue
                    if "401" in message or "invalid_api_key" in message or "invalid api key" in message:
                        break
                    if "429" in message or "rate_limit" in message or "rate limit" in message:
                        pause = _parse_groq_wait_seconds(last_error)
                        _GROQ_COOLDOWN[pair] = time.time() + pause
                        if on_switch:
                            on_switch(f"ضغط على {model_name.split('/')[-1]} (تهدئة {pause}ث) — التبديل للبديل...")
                    continue
        if saw_payload_too_large:
            raise ValueError("413 حجم الحزمة أكبر من سقف النموذج الحالي؛ سيتم التحويل للتحليل الفردي")
        now = time.time()
        all_candidates = [(key, model) for key in keys for model in GROQ_MODELS if (key, model) not in _GROQ_UNAVAILABLE]
        active_waits = [
            (_GROQ_COOLDOWN[pair] - now, pair)
            for pair in all_candidates
            if _GROQ_COOLDOWN.get(pair, 0) > now
        ]
        if not active_waits or len(active_waits) < len(all_candidates):
            break
        min_wait, nearest_pair = min(active_waits, key=lambda item: item[0])
        if min_wait <= 95:
            sleep_sec = max(1, int(min_wait) + 1)
            if on_switch:
                on_switch(f"امتلاء حصة الدقيقة مؤقتاً: انتظار {sleep_sec} ثانية ثم استئناف التحليل تلقائياً...")
            time.sleep(sleep_sec)
            continue
        stored = _GROQ_LAST_ERROR.get(nearest_pair) or last_error
        detail = describe_groq_error(stored) if stored else ""
        raise RuntimeError(
            f"429 استُنفدت حصة Groq اليومية لجميع النماذج المتاحة (أقرب تحديث بعد {max(1, int(min_wait // 60))} دقيقة). {detail}"
        )
    if last_error is not None:
        raise last_error
    raise RuntimeError("429 تعذر الوصول إلى نماذج Groq حالياً")


def test_groq_key(api_key: str) -> str:
    """يختبر المفتاح الممرر تحديداً دون القفز إلى مفاتيح احتياطية أخرى."""
    cleaned = clean_groq_key(api_key)
    if not cleaned:
        keys = collect_groq_keys("")
        cleaned = keys[0] if keys else ""
    if not cleaned or not cleaned.startswith("gsk_"):
        raise RuntimeError("أدخل مفتاح Groq صالحاً يبدأ بـ gsk_")
    client = Groq(api_key=cleaned)
    last_error = None
    for model_name in GROQ_MODELS:
        try:
            completion = _groq_create(
                client,
                model_name,
                [{"role": "user", "content": "رد تم"}],
                temperature=0,
                use_format=False,
            )
            return str(completion.choices[0].message.content or "").strip()
        except Exception as exc:
            last_error = exc
            if _is_model_unavailable(str(exc)):
                continue
            raise
    raise last_error or RuntimeError("تعذر اختبار المفتاح عبر نماذج Groq")


def text_mentions_shs(text: str) -> bool:
    """يتعرف على منظومة SHS والهايبرد من دون التقاط البطارية أو الشاحن العادي."""
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in SHS_MODEL_KEYWORDS)


def _mentions_model(text: str, model: str) -> bool:
    lowered = (text or "").lower()
    code = {"Jaecoo J5": "j5", "Jaecoo J7": "j7", "Jaecoo J8": "j8"}[model]
    if re.search(rf"(?<![a-z0-9]){code}(?![a-z0-9])", lowered):
        return True
    return any(marker in text for marker in _MODEL_MARKERS[model])


def _card_blob(data: dict, source_text: str = "") -> str:
    if not isinstance(data, dict):
        return source_text or ""
    return "\n".join(
        str(part or "")
        for part in (
            source_text,
            data.get("title"),
            data.get("problem_or_topic"),
            data.get("solution_or_summary"),
            data.get("keywords"),
            data.get("part_numbers_or_specs"),
        )
    )


def _mentions_hybrid(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ("هايبرد", "هجين", "phev", "بلق ان", "بلقن", "plug-in", "plugin"))


def _mentions_shs_name(text: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])shs(?![a-z0-9])", (text or "").lower()))


def assign_jaecoo_model(data: dict, source_text: str = "") -> dict:
    """J7/J5 الهايبرد تبقى لطرازها. J8 SHS عند SHS أو هايبرد J8 أو هايبرد بلا طراز."""
    if not isinstance(data, dict):
        return data
    blob = _card_blob(data, source_text)
    has_j5 = _mentions_model(blob, "Jaecoo J5")
    has_j7 = _mentions_model(blob, "Jaecoo J7")
    has_j8 = _mentions_model(blob, "Jaecoo J8")
    hybrid = _mentions_hybrid(blob)
    shs_name = _mentions_shs_name(blob)
    if shs_name or (hybrid and has_j8) or (hybrid and not has_j7 and not has_j5):
        data["car_model"] = "Jaecoo J8 SHS"
    elif has_j8:
        data["car_model"] = "Jaecoo J8"
    elif has_j7:
        data["car_model"] = "Jaecoo J7"
    elif has_j5:
        data["car_model"] = "Jaecoo J5"
    return data


def _field_is_blank(value) -> bool:
    return str(value or "").strip().lower() in {"", "nan", "none", "null"}


def _has_useful_info(text: str) -> bool:
    cleaned = str(text or "").strip()
    if _field_is_blank(cleaned):
        return False
    if "?" in cleaned or "؟" in cleaned:
        return True
    lowered = cleaned.lower()
    return any(marker.lower() in lowered for marker in USEFUL_TOPIC_TERMS)


def card_passes_quality_gate(card: dict) -> bool:
    """تقبل البطاقة العربية ذات العنوان والحل، وترفض فقط الدردشة الفارغة صراحةً."""
    return not card_rejection_reason(card)


def card_rejection_reason(card: dict) -> str:
    if not isinstance(card, dict):
        return "البطاقة ليست بيانات صالحة"
    title = str(card.get("title") or "").strip()
    problem = str(card.get("problem_or_topic") or "").strip()
    solution = str(card.get("solution_or_summary") or "").strip()
    if _field_is_blank(title):
        return "العنوان فارغ"
    if _field_is_blank(solution):
        return "الحل فارغ"
    if not re.search(r"[\u0600-\u06FF]", title + problem + solution):
        return "لا يوجد نص عربي في العنوان أو الحل"
    flag = card.get("is_worth_archiving", None)
    explicit_false = flag in (False, 0, "0", "false", "False")
    if explicit_false and not _has_useful_info("\n".join((title, problem, solution))):
        return "صُنفت دردشة فارغة بلا فائدة فنية أو تجربة مالك"
    return ""


def merge_missing_links(card: dict, source_text: str) -> dict:
    """يضيف روابط الرسالة إلى الحل أو القطع إذا أسقطها التلخيص."""
    if not isinstance(card, dict):
        return card
    urls = []
    for raw_url in extract_urls(source_text or ""):
        url = raw_url.rstrip(".,:;!?،؛])")
        if url and url not in urls:
            urls.append(url)
    if not urls:
        return card
    solution = str(card.get("solution_or_summary") or "")
    parts = str(card.get("part_numbers_or_specs") or "")
    missing = [url for url in urls if url not in solution and url not in parts]
    if not missing:
        return card
    extra = " | ".join(missing)
    if _field_is_blank(parts) or parts.strip() == "غير محدد":
        card["part_numbers_or_specs"] = extra
    else:
        card["solution_or_summary"] = f"{solution}\n{extra}".strip()
    return card


def force_j8_shs_model(data: dict, source_text: str = "") -> dict:
    return assign_jaecoo_model(data, source_text)


def _normalize_card_fields(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    if "is_worth_archiving" not in data and "is_useful" in data:
        data["is_worth_archiving"] = data.get("is_useful")
    aliases = {
        "model": "car_model",
        "problem": "problem_or_topic",
        "solution": "solution_or_summary",
        "parts": "part_numbers_or_specs",
        "subsystem": "sub_system",
        "verified": "is_verified_solution",
    }
    for source, target in aliases.items():
        if data.get(source) not in (None, "") and not data.get(target):
            data[target] = data[source]
    if data.get("sub_system") == "المحرك والتيربو":
        data["sub_system"] = "المحرك والتيربو والمنظومة الهجين SHS"
    return data


def _strip_model_json(raw: str) -> str:
    """يزيل وسم think المغلق والمفتوح ثم يأخذ أول كائن JSON."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think>[\s\S]*?(?=\{)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        return match.group(0)
    return text.strip()


def _loads_model_json(raw: str) -> dict:
    payload = _strip_model_json(raw)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", payload)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("رد النموذج ليس كائن JSON")
    return parsed


def _has_confirmed_solution_signal(thread_text: str) -> Optional[bool]:
    """يفحص عبارات نجاح الحل مع استبعاد صيغ النفي مثل (ما انحلت / لم يشتغل)."""
    raw = str(thread_text or "")
    if not raw.strip():
        return False
    negated_pattern = rf"{_NEGATION_PREFIX}(?:{_CONFIRM_WORDS_PATTERN})"
    has_negated = bool(re.search(negated_pattern, raw))
    scrubbed = re.sub(negated_pattern, " ", raw)
    has_positive = bool(re.search(rf"(?:{_CONFIRM_WORDS_PATTERN})", scrubbed))
    if has_positive:
        return True
    if has_negated:
        return False
    return None


def analyze_thread_with_ai(
    thread_text: str,
    api_key: str,
    media_bytes=None,
    mime_type=None,
    on_switch=None,
    group_name: str = "",
) -> dict:
    dealer_hint = (
        "\nتنبيه: هذه الرسالة تتعلق بالوكيل أو الصيانة الدورية؛ اجعل is_worth_archiving = true."
        if is_dealer_or_maintenance_msg(thread_text)
        else ""
    )
    prompt = f"""{SYSTEM_PROMPT}{dealer_hint}
أخرج JSON مطابقاً لهذا القالب حصراً:
{COMPACT_SCHEMA_GUIDE}
النص المراد تحليله:
{thread_text}"""
    messages = [
        {
            "role": "system",
            "content": (
                "Return valid JSON only. أخرج كائن JSON فقط بالعربية دون أي شرح خارجي أو وسم think. "
                "is_verified_solution = true فقط إذا أكد عضو نجاح الحل فعلياً."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    last_error = None
    data = None
    for json_mode in (True, False):
        try:
            completion = _groq_complete(api_key, messages, json_mode=json_mode, on_switch=on_switch)
            data = _normalize_card_fields(_loads_model_json(completion.choices[0].message.content or ""))
            break
        except Exception as exc:
            last_error = exc
            if json_mode and not is_key_or_quota_error(exc):
                continue
            raise
    if data is None:
        raise last_error or RuntimeError("تعذر قراءة JSON من رد Groq")
    return _hint_model_from_group(_polish_extracted_card(data, thread_text), thread_text, group_name)


def _polish_extracted_card(data: dict, thread_text: str) -> dict:
    data = _normalize_card_fields(data)
    parts = str(data.get("part_numbers_or_specs") or "").strip()
    if parts.lower() in {"", "nan", "none", "null"}:
        data["part_numbers_or_specs"] = "غير محدد"
    problem_text = str(data.get("problem_or_topic") or "")
    solution_text = str(data.get("solution_or_summary") or "")
    has_arabic = bool(re.search(r"[\u0600-\u06FF]", problem_text + solution_text))
    has_product = is_product_link_text(thread_text + "\n" + solution_text + "\n" + parts)
    if text_mentions_shs("\n".join((thread_text, problem_text, solution_text, parts))) or any(
        marker in thread_text for marker in ("ترصيص", "جدول الصيانة", "جدول صيانة")
    ):
        data["is_worth_archiving"] = True
    if is_dealer_or_maintenance_msg(thread_text) and has_arabic:
        data["is_worth_archiving"] = True
        if str(data.get("category") or "") in {"", "نبض القروب والدردشة العامة"}:
            data["category"] = "الصيانة الدورية والوكالات"
    elif has_product and (has_arabic or extract_urls(solution_text + "\n" + parts)):
        data["is_worth_archiving"] = True
        if str(data.get("category") or "") in {"", "نبض القروب والدردشة العامة"}:
            data["category"] = "الإكسسوارات والتجهيزات"
        if str(data.get("sub_system") or "") in {"", "عام"}:
            data["sub_system"] = "الإكسسوارات ومنتجات AliExpress"
        if parts.lower() in {"", "غير محدد", "nan", "none", "null"}:
            product_urls = [url for url in extract_urls(thread_text) if "aliexpress" in url.lower()]
            if product_urls:
                data["part_numbers_or_specs"] = product_urls[0]
    elif is_owner_experience_msg(thread_text) and has_arabic:
        data["is_worth_archiving"] = True
        if str(data.get("category") or "") in {"", "نبض القروب والدردشة العامة"}:
            data["category"] = "تجارب القيادة والأداء الفعلي"
        if str(data.get("sub_system") or "") in {"", "عام"}:
            data["sub_system"] = "تقييم العملية والمميزات والعيوب"
    elif (
        data.get("is_worth_archiving")
        and not has_arabic
        and not has_product
        and not text_mentions_shs("\n".join((thread_text, problem_text, solution_text, parts)))
    ):
        data["is_worth_archiving"] = False
    if not str(data.get("solution_or_summary") or "").strip() and problem_text.strip():
        data["solution_or_summary"] = problem_text.strip()
    if _has_useful_info(thread_text) or _has_useful_info(problem_text) or _has_useful_info(str(data.get("solution_or_summary") or "")):
        data["is_worth_archiving"] = True
    signal = _has_confirmed_solution_signal(thread_text)
    if signal is True:
        data["is_verified_solution"] = True
    elif signal is False:
        data["is_verified_solution"] = False
    else:
        has_replies = "الرد رقم" in (thread_text or "")
        is_unanswered_q = ("؟" in thread_text or "?" in thread_text) and not has_replies
        data["is_verified_solution"] = False if is_unanswered_q else bool(data.get("is_verified_solution"))
    return data


def _hint_model_from_group(card: dict, thread_text: str, group_name: str = "") -> dict:
    """إذا كان القروب يخص J8 ولم يُذكر J5 أو J7 أو J8 في النقاش، تُنسب البطاقة إلى J8."""
    card = assign_jaecoo_model(card, thread_text)
    blob = _card_blob(card, thread_text)
    named = any(_mentions_model(blob, model) for model in ("Jaecoo J5", "Jaecoo J7", "Jaecoo J8"))
    if named or not _mentions_model(group_name or "", "Jaecoo J8"):
        return card
    current = str(card.get("car_model") or "").strip()
    if current not in {"", "جميع الطرازات / عام"}:
        return card
    card["car_model"] = "Jaecoo J8 SHS" if (_mentions_hybrid(blob) or _mentions_shs_name(blob)) else "Jaecoo J8"
    return card


def _prepare_batch_card(raw: dict, thread_text: str, thread_id, group_name: str) -> dict:
    card = _hint_model_from_group(_polish_extracted_card(raw, thread_text), thread_text, group_name)
    card = merge_missing_links(card, thread_text)
    card["thread_id"] = thread_id
    return card


def analyze_threads_batch_with_ai(threads_payload: list[dict], api_key: str, group_name: str = "", on_switch=None) -> list[dict]:
    payload = [item for item in (threads_payload or []) if str(item.get("text") or "").strip()][:4]
    if not payload:
        return []

    def _fallback_one(item: dict) -> dict:
        thread_id = item.get("thread_id")
        trimmed_text = str(item.get("text") or "")[:3500]
        try:
            card = analyze_thread_with_ai(trimmed_text, api_key, on_switch=on_switch, group_name=group_name)
            return _prepare_batch_card(card, str(item.get("text") or ""), thread_id, group_name)
        except Exception as exc:
            return {
                "thread_id": thread_id,
                "_error_type": type(exc).__name__,
                "_error": str(exc),
                "_key_failure": is_key_or_quota_error(exc),
            }

    def _fallback_all() -> list[dict]:
        return [_fallback_one(item) for item in payload]

    packed = "\n\n".join(
        f"### THREAD {item.get('thread_id')}\n{str(item.get('text') or '')[:2200]}"
        for item in payload
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Return valid JSON only. أعد كائناً واحداً بالصيغة "
                '{"cards": [{"thread_id": 0, "is_worth_archiving": true, "car_model": "", "category": "", '
                '"sub_system": "", "title": "", "problem_or_topic": "", "solution_or_summary": "", '
                '"part_numbers_or_specs": "", "is_verified_solution": false, "keywords": ""}]} '
                "بطاقة واحدة لكل THREAD مع نسخ thread_id كما هو وبالعربية الفصحى. "
                "Jaecoo J7 مع هايبرد تبقى J7 إذا لم يُذكر J8 أو SHS. "
                "Jaecoo J8 SHS عند SHS أو هايبرد J8 أو هايبرد بلا J7 وJ5."
            ),
        },
        {"role": "user", "content": f"حلّل النقاشات التالية وأعد JSON:\n{packed}"},
    ]
    try:
        completion = _groq_complete(api_key, messages, json_mode=True, on_switch=on_switch)
        parsed = _loads_model_json(completion.choices[0].message.content or "")
        raw_cards = parsed.get("cards") if isinstance(parsed, dict) else None
        if not isinstance(raw_cards, list):
            return _fallback_all()
    except Exception as batch_exc:
        if is_key_or_quota_error(batch_exc):
            return [{
                "thread_id": item.get("thread_id"),
                "_error_type": type(batch_exc).__name__,
                "_error": str(batch_exc),
                "_key_failure": True,
            } for item in payload]
        return _fallback_all()

    by_id = {}
    for raw in raw_cards:
        if isinstance(raw, dict) and raw.get("thread_id") is not None:
            by_id[str(raw.get("thread_id"))] = raw
    results = []
    for item in payload:
        thread_id = item.get("thread_id")
        raw = by_id.get(str(thread_id))
        if not isinstance(raw, dict):
            results.append(_fallback_one(item))
            continue
        try:
            results.append(_prepare_batch_card(raw, str(item.get("text") or ""), thread_id, group_name))
        except Exception:
            results.append(_fallback_one(item))
    return results


VISION_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]
WHISPER_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]


def analyze_image_bytes(api_key: str, image_bytes: bytes, caption: str = "", mime_type: str = "image/jpeg") -> str:
    """يحلل صورة الطبلون أو القطعة أو الفاتورة مع التبديل بين مفاتيح ونماذج Groq."""
    payload = base64.b64encode(image_bytes).decode("utf-8")
    data_mime = "image/jpeg" if mime_type == "video/thumb" else (mime_type or "image/jpeg")
    if mime_type == "video/thumb":
        prompt = "صف بالعربية ما يظهر في لقطة الفيديو هذه لسيارة جايكو: هل يظهر عطل في الشاشة أو المحرك أم شرح لقطعة وإصلاح؟"
    else:
        prompt = (
            "افحص هذه الصورة المرسلة في قروب سيارات جايكو (Jaecoo) بدقة وبالعربية. "
            "إذا كانت منتجاً أو إكسسواراً من علي إكسبريس أو متجر إلكتروني، فابدأ بـ [صورة منتج/إكسسوار]: واذكر الاسم والفائدة والسعر إن ظهر. "
            "إذا كانت توضح مشكلة أو عطلاً، مثل لمبة تحذير أو رسالة خطأ أو كسر أو تهريب، فابدأ بـ [صورة توضح مشكلة]: واشرح العطل. "
            "إذا كانت قطعة غيار أو جدول صيانة أو حلاً فنياً، فابدأ بـ [صورة توضح حلاً/معلومة]: واستخرج البيانات والأرقام. "
            "إذا ظهرت منظومة Jaecoo J8 SHS الهجينة، أو شعار SHS، أو منفذ الشحن، أو البطارية الكبيرة، أو وضع الهايبرد، أو جدول صيانة أو تجربة ترصيص، "
            "فاذكر الطراز والكلمات الظاهرة كما هي: SHS أو هايبرد أو هجين أو البطارية الكبيرة. "
            "بطارية 12 فولت وشاحن الجوال لا يعنيان Jaecoo J8 SHS. "
            f"تعليق العضو المرفق مع الصورة: {caption}"
        )
    keys = collect_groq_keys(api_key)
    if not keys:
        raise RuntimeError("401 مفتاح Groq API غير موجود")
    last_error = None
    now = time.time()
    for key in keys:
        client = Groq(api_key=key)
        for model_name in VISION_MODELS:
            if _GROQ_COOLDOWN.get((key, model_name), 0) > now:
                continue
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{data_mime};base64,{payload}"}},
                        ],
                    }],
                    temperature=0.2,
                )
                return str(response.choices[0].message.content or "").strip()
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                if _is_model_unavailable(msg):
                    _GROQ_COOLDOWN[(key, model_name)] = time.time() + _GROQ_MISSING_MODEL_COOLDOWN
                elif "429" in msg or "rate_limit" in msg:
                    _GROQ_COOLDOWN[(key, model_name)] = time.time() + _parse_groq_wait_seconds(exc)
                elif "401" in msg or "invalid_api_key" in msg:
                    break
                continue
    raise last_error or RuntimeError("تعذر تحليل الصورة")


def transcribe_media_bytes(api_key: str, media_bytes: bytes, ext: str = ".ogg") -> str:
    """يفرّغ المقطع الصوتي أو صوت الفيديو من الذاكرة مباشرة مع دوران المفاتيح."""
    suffix = ext if ext.startswith(".") else f".{ext}"
    keys = collect_groq_keys(api_key)
    if not keys:
        raise RuntimeError("401 مفتاح Groq API غير موجود")
    last_error = None
    now = time.time()
    for key in keys:
        client = Groq(api_key=key)
        for model_name in WHISPER_MODELS:
            if _GROQ_COOLDOWN.get((key, model_name), 0) > now:
                continue
            try:
                transcription = client.audio.transcriptions.create(
                    file=(f"media{suffix}", media_bytes),
                    model=model_name,
                    language="ar",
                    response_format="text",
                )
                return str(transcription).strip()
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                if _is_model_unavailable(msg):
                    _GROQ_COOLDOWN[(key, model_name)] = time.time() + _GROQ_MISSING_MODEL_COOLDOWN
                    continue
                if "429" in msg or "rate_limit" in msg:
                    _GROQ_COOLDOWN[(key, model_name)] = time.time() + _parse_groq_wait_seconds(exc)
                    continue
                if "401" in msg or "invalid_api_key" in msg:
                    break
                raise
    raise last_error or RuntimeError("تعذر تفريغ المقطع الصوتي")


def find_matching_solution_in_db(question_text: str, api_key: str) -> Optional[str]:
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT title, car_model, problem_or_topic, solution_or_summary, part_numbers_or_specs, author_name "
        "FROM knowledge_cards WHERE is_verified_solution = 1 ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()

    if not rows:
        return None

    archive_context = "\n".join([
        f"- [{r[1]}] {r[0]} | المشكلة: {r[2]} | الحل: {r[3]} | القطع: {r[4]} (مشاركة: {r[5]})"
        for r in rows
    ])

    completion = _groq_complete(
        api_key,
        [
            {"role": "system", "content": "أنت مساعد استشاري لقروب ملاك جايكو."},
            {"role": "user", "content": f"الأرشيف المعتمد:\n{archive_context}\n\nسؤال العضو الحالي:\n{question_text}\n\nإذا كان السؤال موجوداً في الأرشيف، اكتب إجابة مختصرة تبدأ بـ '💠 أهلاً بك! تمت مناقشة هذا الموضوع سابقاً في القروب وإليك الخلاصة المعتمدة:' مع ذكر الحل واسم صاحب التجربة. وإذا لم يوجد، اكتب NO_MATCH فقط."},
        ],
    )
    reply = completion.choices[0].message.content.strip()
    return None if "NO_MATCH" in reply else reply


def generate_daily_pulse_summary(api_key: str) -> str:
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT category, car_model, title, solution_or_summary, author_name FROM knowledge_cards ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()

    if not rows:
        return "لا توجد بيانات كافية لتوليد الملخص."

    items = "\n".join([f"- ({r[0]} | {r[1]}) {r[2]}: {r[3]} [بواسطة: {r[4]}]" for r in rows])
    completion = _groq_complete(
        api_key,
        [
            {"role": "system", "content": "أنت محرر تقني لنشرة القروب."},
            {"role": "user", "content": f"بناءً على أحدث المواضيع الموثقة:\n{items}\n\nصِغ نشرة موجزة بعنوان '⚡ الموجز الذكي لنبض قروب جايكو'."},
        ],
    )
    return completion.choices[0].message.content