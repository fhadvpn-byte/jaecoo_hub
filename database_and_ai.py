import base64
import io
import json
import os
import re
import sqlite3
import tempfile
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
        description="True لمعلومة فنية أو لبيانات الوكيل والصيانة الدورية والضمان. False للرسائل العامة والتحيات والأسئلة بلا إجابة."
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

المجال الأول: منتجات وإكسسوارات علي إكسبريس والتجهيزات.
- أي رابط aliexpress.com أو s.click.aliexpress أو صورة إكسسوار (كفر مفتاح، دعاسات، شاحن، داش كام، حماية شاشة، ستائر، منظم، فلاتر) يعني is_worth_archiving = true.
- category = الإكسسوارات والتجهيزات.
- sub_system = الإكسسوارات ومنتجات AliExpress.
- problem_or_topic = اسم الإكسسوار والغرض منه لسيارة جايكو.
- solution_or_summary = المواصفات وانطباع العضو، مع إبقاء رابط الشراء كما هو.
- part_numbers_or_specs = الرابط المباشر أو اسم القطعة.

المجال الثاني: الاعتمادية والعملية والمميزات والعيوب من تجارب الملاك.
- إذا دار النقاش عن عملية السيارة أو مميزاتها أو عيوبها أو تجربة بعد ممشى أو العزل أو التكييف أو الصرفية أو رساوة الخط، اجعل is_worth_archiving = true.
- category = تجارب القيادة والأداء الفعلي.
- sub_system = تقييم العملية والمميزات والعيوب.
- problem_or_topic = محور التقييم.
- solution_or_summary = أولاً العملية والاعتمادية، ثم أبرز المميزات، ثم أبرز العيوب.

المجال الثالث: الوكيل والصيانة الدورية المعتمدة.
- الوكيل الرسمي، الفروع، المدن، الضمان، المراكز المعتمدة، وتكاليف صيانة 5000 و10000 و20000 كم.
- category = الصيانة الدورية والوكالات.
- sub_system = الوكيل والمراكز المعتمدة أو جدول وتكاليف الصيانة الدورية.
- part_numbers_or_specs = الزيت أو الفلتر أو اسم الفرع، وإلا فاكتب غير محدد.

المجال الرابع: الأعطال والحلول والشاشات والقطع البديلة.
- صورة أو صوت أو فيديو العطل يذهب إلى problem_or_topic، وشرح الحل أو رقم القطعة أو جدول الصيانة يذهب إلى solution_or_summary.

شروط صارمة:
1. الحقول النصية بالعربية الفصحى فقط. يُسمح بروابط الإنترنت وأرقام القطع وأسماء الطرازات.
2. ممنوع الجمل الإنجليزية وممنوع nan.
3. السؤال العابر بلا إجابة وبلا رابط منتج وبلا معلومة يعني is_worth_archiving = false. لا تكرر السؤال في خانة الحل.
4. car_model أحد: Jaecoo J8، Jaecoo J8 SHS، Jaecoo J7، Jaecoo J5، جميع الطرازات / عام.
5. أي ذكر لـ SHS أو هجين أو هايبرد أو بطارية أو شاحن يُصنَّف فوراً car_model = Jaecoo J8 SHS، وليس Jaecoo J8 ولا جميع الطرازات / عام.
"""

# ==========================================
# 3. إدارة قاعدة البيانات (SQLite + WAL)
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
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
    conn.commit()
    conn.close()


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
    cfg = load_config()
    cfg["tg_session"] = session_text.strip()
    save_config(cfg)


def mark_telegram_session_invalid() -> None:
    set_app_setting("telegram_session_status", "invalid")


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


def save_card(
    card_data: dict,
    telegram_msg_id: int,
    thread_url: str,
    author_name: str,
    raw_text: str,
    media_type: str = "text",
    media_b64: str = "",
):
    conn = get_db_connection()
    conn.execute("""
        INSERT OR REPLACE INTO knowledge_cards (
            telegram_msg_id, thread_url, category, car_model, sub_system,
            title, problem_or_topic, solution_or_summary, part_numbers_or_specs,
            is_verified_solution, keywords, author_name, raw_thread_text, created_at,
            media_type, media_b64
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_msg_id,
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
    ))
    conn.commit()
    conn.close()

# ==========================================
# 4. محرك Groq الذكي والسريع
# ==========================================
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
]
CAR_THREAD_KEYWORDS = [
    "جايكو", "j8", "j7", "j5", "shs", "وكيل", "وكالة", "صيانة", "معتمد", "ضمان",
    "عطل", "مشكلة", "حل", "قطعة", "قطع", "سعر", "زيت", "فلتر", "بطارية", "شاشة",
    "حساس", "قير", "مكينة", "تيربو", "مكيف", "تكييف", "عزل", "عملية", "عمليه",
    "مميزات", "عيوب", "تجربة", "تجربتي", "علي اكسبرس", "aliexpress", "http",
    "فرع", "تحديث", "برمجة", "كاربلاي", "اندرويد", "صوت", "طقطقة", "رجة", "حرارة",
    "فرامل", "فحمات", "مساعدات", "كفرات", "بنزين", "صرفية", "دبل", "رادار", "كاميرا",
    "هايبرد", "هجين", "الهجينة", "الكهربائية", "الشاحن", "محرك الشحن", "البطارية الكبيرة", "الفيش",
]
SHS_MODEL_KEYWORDS = [
    "shs", "hybrid", "phev", "هايبرد", "هجين", "بطارية", "الكهربائية", "الشاحن", "شاحن",
    "محرك الشحن", "البطارية الكبيرة", "الفيش", "فيش",
]
EXPERT_GUIDELINES_TOPICS = [
    "خطوات", "استلام", "الوكيل", "المعرض", "فحص", "تشييك", "قبل الخروج",
    "دليل", "نصائح", "تعليمات", "ملاحظات", "الترخيص", "اللوحات", "التأمين",
    "تلميع", "حماية", "تظليل", "عزل", "ممشى", "تمرين", "الاطارات", "الإطارات",
    "الكاوتشوك", "تشيك",
]


def has_analyzable_text(text: str) -> bool:
    """يكفي أن يزيد النص على خمس كلمات حتى يُرسل إلى الذكاء الاصطناعي."""
    words = [word for word in re.split(r"\s+", (text or "").strip()) if word]
    return len(words) > 5


def is_car_related_thread(text: str, media_type: str = "text") -> bool:
    """يمرر الصور والفيديو والصوت، وأي نص أطول من خمس كلمات، دون فلتر كلمات محلي."""
    if media_type in {"image", "audio", "video"}:
        return True
    return has_analyzable_text(text)


def should_analyze_thread(text: str, media_type: str = "text") -> bool:
    return is_car_related_thread(text, media_type)


def collect_groq_keys(primary: str = "") -> list[str]:
    """يجمع المفتاح الأساسي والمفاتيح الإضافية، ولكل مفتاح رصيده المستقل."""
    cfg = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    keys = []
    for value in (
        primary,
        cfg.get("groq_api_key"),
        cfg.get("gemini_api_key"),
        cfg.get("groq_api_key_2"),
        cfg.get("groq_api_key_3"),
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("GROQ_API_KEY_2"),
        os.environ.get("GROQ_API_KEY_3"),
    ):
        text = str(value or "").strip()
        if text and text not in keys:
            keys.append(text)
    return keys


def _groq_complete(api_key: str, messages: list, temperature: float = 0.1, json_mode: bool = False, on_switch=None):
    """يجرّب المفاتيح والنماذج بالتتابع. عند 429 ينتقل فوراً دون انتظار تهدئة طويلة."""
    keys = collect_groq_keys(api_key)
    if not keys:
        raise RuntimeError("مفتاح Groq API غير موجود")
    last_error = None
    for key in keys:
        client = Groq(api_key=key)
        for model_name in GROQ_MODELS:
            kwargs = {"model": model_name, "messages": messages, "temperature": temperature}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if on_switch and ("429" in message or "rate_limit" in message):
                    on_switch(f"تبديل فوري إلى النموذج البديل ({model_name.split('/')[-1]}) لمواصلة السحب بدون انتظار.")
                if json_mode and "response_format" in message:
                    kwargs.pop("response_format", None)
                    try:
                        return client.chat.completions.create(**kwargs)
                    except Exception as retry_exc:
                        last_error = retry_exc
                continue
    time.sleep(5)
    raise last_error or RuntimeError("تعذر التحليل عبر نماذج Groq")


def text_mentions_shs(text: str) -> bool:
    """يتعرف على اختصارات منظومة SHS حتى لو لم تُكتب الكلمة كاملة."""
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in SHS_MODEL_KEYWORDS)


def force_j8_shs_model(data: dict, source_text: str = "") -> dict:
    """يثبت طراز Jaecoo J8 SHS عندما يخص النقاش أو الصورة المنظومة الهجينة."""
    if not isinstance(data, dict):
        return data
    blob = "\n".join(
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
    if not text_mentions_shs(blob):
        return data
    data["car_model"] = "Jaecoo J8 SHS"
    return data


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
    return data


def analyze_thread_with_ai(thread_text: str, api_key: str, media_bytes=None, mime_type=None, on_switch=None) -> dict:
    schema_json_str = json.dumps(KnowledgeCardSchema.model_json_schema(), ensure_ascii=False)
    dealer_hint = ""
    if is_dealer_or_maintenance_msg(thread_text):
        dealer_hint = """
تنبيه هام: هذه الرسالة تتضمن معلومات عن الوكيل أو الصيانة الدورية المعتمدة.
اجعل is_worth_archiving = true وصنّفها في قسم الصيانة الدورية والوكالات.
"""
    prompt = f"""
{SYSTEM_PROMPT}
{dealer_hint}
يرجى تحليل النص التالي واستخراج النتيجة بصيغة JSON مطابقة تماماً لهذا المخطط (Schema):
{schema_json_str}

النص المراد تحليله:
{thread_text}
"""

    completion = _groq_complete(
        api_key,
        [
            {
                "role": "system",
                "content": (
                    "استخرج JSON فقط وبالعربية الفصحى. "
                    "احفظ روابط علي إكسبريس والإكسسوارات، وتجارب العملية والمميزات والعيوب، وبيانات الوكيل والصيانة. "
                    "صورة أو صوت أو فيديو العطل يذهب إلى problem_or_topic، وشرح الحل أو المنتج يذهب إلى solution_or_summary. "
                    "أبقِ رابط الشراء كما هو داخل solution_or_summary أو part_numbers_or_specs. "
                    "السؤال بلا رد وبلا رابط منتج يعني is_worth_archiving = false. "
                    "أي ذكر لـ SHS أو هجين أو هايبرد أو بطارية أو شاحن يعني car_model = Jaecoo J8 SHS فوراً. "
                    "ممنوع إخراج nan."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
        on_switch=on_switch,
    )

    data = _normalize_card_fields(json.loads(completion.choices[0].message.content))
    parts = str(data.get("part_numbers_or_specs") or "").strip()
    if parts.lower() in {"", "nan", "none", "null"}:
        data["part_numbers_or_specs"] = "غير محدد"
    problem_text = str(data.get("problem_or_topic") or "")
    solution_text = str(data.get("solution_or_summary") or "")
    has_arabic = bool(re.search(r"[\u0600-\u06FF]", problem_text + solution_text))
    has_product = is_product_link_text(thread_text + "\n" + solution_text + "\n" + parts)
    question_only = _solution_repeats_question(problem_text, solution_text)
    if question_only and not has_product:
        data["is_worth_archiving"] = False
        return force_j8_shs_model(data, thread_text)
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
    elif is_owner_experience_msg(thread_text) and has_arabic and not question_only:
        data["is_worth_archiving"] = True
        if str(data.get("category") or "") in {"", "نبض القروب والدردشة العامة"}:
            data["category"] = "تجارب القيادة والأداء الفعلي"
        if str(data.get("sub_system") or "") in {"", "عام"}:
            data["sub_system"] = "تقييم العملية والمميزات والعيوب"
    elif data.get("is_worth_archiving") and not re.search(r"[\u0600-\u06FF]", problem_text):
        data["is_worth_archiving"] = False
    if question_only and not has_product:
        data["is_worth_archiving"] = False
    return force_j8_shs_model(data, thread_text)


def _solution_repeats_question(problem_text: str, solution_text: str) -> bool:
    solution = solution_text.strip()
    problem = problem_text.strip()
    if any(phrase in solution for phrase in ("الرسالة تطلب معلومات", "لا يوجد حل محدد", "لا يوجد حل")):
        return True
    return bool(solution) and solution == problem


VISION_MODELS = ["meta-llama/llama-4-scout-17b-16e-instruct"]
WHISPER_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]


def analyze_image_bytes(api_key: str, image_bytes: bytes, caption: str = "", mime_type: str = "image/jpeg") -> str:
    """يحلل صورة الطبلون أو القطعة أو الفاتورة ويعيد وصفاً عربياً."""
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
            "إذا ظهرت منظومة Jaecoo J8 SHS الهجينة، أو شعار SHS، أو منفذ الشحن (الفيش)، أو البطارية الكبيرة، أو وضع الهايبرد، "
            "فاذكر ذلك صراحة بالكلمات: SHS، هجين، هايبرد، بطارية، شاحن. "
            f"تعليق العضو المرفق مع الصورة: {caption}"
        )
    client = Groq(api_key=api_key)
    last_error = None
    for model_name in VISION_MODELS:
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
            if "404" in str(exc) or "model_not_found" in str(exc):
                continue
            raise
    raise last_error


def transcribe_media_bytes(api_key: str, media_bytes: bytes, ext: str = ".ogg") -> str:
    """يفرّغ المقطع الصوتي أو صوت الفيديو إلى نص عربي."""
    suffix = ext if ext.startswith(".") else f".{ext}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(media_bytes)
        tmp_path = tmp.name
    try:
        client = Groq(api_key=api_key)
        last_error = None
        for model_name in WHISPER_MODELS:
            try:
                with open(tmp_path, "rb") as handle:
                    transcription = client.audio.transcriptions.create(
                        file=(f"media{suffix}", handle.read()),
                        model=model_name,
                        language="ar",
                        response_format="text",
                    )
                return str(transcription).strip()
            except Exception as exc:
                last_error = exc
                if "404" in str(exc) or "model_not_found" in str(exc):
                    continue
                raise
        raise last_error
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


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