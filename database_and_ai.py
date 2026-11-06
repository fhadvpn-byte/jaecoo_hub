import base64
import io
import json
import os
import re
import sqlite3
import tempfile
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


SYSTEM_PROMPT = """
أنت خبير فني ومستشار خدمات ما بعد البيع لسيارات جايكو (Jaecoo).
استخرج بطاقة معرفية بالعربية الفصحى في مجالين:

المجال الأول وله الأولوية: الوكيل والصيانة الدورية المعتمدة.
- الوكيل الرسمي، الفروع، المدن، التواصل، وحجز المواعيد.
- المراكز المعتمدة للصيانة الدورية داخل المدن وخارجها بما لا يسقط الضمان.
- تكاليف صيانة 5000 و10000 و20000 كم، نوع الزيت، والقطع التي تُبدّل.
- تجارب الضمان واستبدال القطع لدى الوكيل.
- اجعل category = الصيانة الدورية والوكالات.
- problem_or_topic هو الاستفسار، مثل تكلفة صيانة 10000 كم أو مراكز الصيانة المعتمدة.
- solution_or_summary هو التفاصيل: اسم الوكيل أو المركز والمدينة والسعر والإجراء.
- part_numbers_or_specs هو الزيت أو الفلتر أو اسم الفرع، وإلا فاكتب غير محدد.
- sub_system يكون الوكيل والمراكز المعتمدة أو جدول وتكاليف الصيانة الدورية.

المجال الثاني: الأعطال، القطع البديلة، الشاشات، والتجهيزات.
- الأعطال وحلولها، أرقام القطع، برمجة الشاشات، والإكسسوارات المجربة.

شروط صارمة:
1. الحقول النصية بالعربية الفصحى فقط. ممنوع الجمل الإنجليزية وممنوع nan.
2. يُسمح باسم الطراز أو رقم القطعة داخل جملة عربية.
3. السؤال بلا إجابة والدردشة العامة تعني is_worth_archiving = false. لا تتجاهل رسالة الوكيل أو الصيانة إذا كانت فيها معلومة.
4. car_model أحد: Jaecoo J8، Jaecoo J8 SHS، Jaecoo J7، Jaecoo J5، جميع الطرازات / عام.
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
    conn.commit()
    conn.close()

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
GROQ_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-20b", "qwen/qwen3.8-27b", "allam-2-7b"]


def _groq_complete(api_key: str, messages: list, temperature: float = 0.1, json_mode: bool = False):
    client = Groq(api_key=api_key)
    last_error = None
    for model_name in GROQ_MODELS:
        kwargs = {"model": model_name, "messages": messages, "temperature": temperature}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if json_mode and "response_format" in message.lower():
                kwargs.pop("response_format", None)
                try:
                    return client.chat.completions.create(**kwargs)
                except Exception as retry_exc:
                    last_error = retry_exc
                    message = str(retry_exc)
            if "404" in message or "model_not_found" in message or "does not exist" in message:
                continue
            raise
    raise last_error


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


def analyze_thread_with_ai(thread_text: str, api_key: str, media_bytes=None, mime_type=None) -> dict:
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
                    "رسائل الوكيل والصيانة الدورية والضمان تُحفظ ولا تُتجاهل. "
                    "السؤال بلا إجابة والدردشة العامة تعني is_worth_archiving = false. "
                    "ممنوع إخراج nan."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
    )

    data = _normalize_card_fields(json.loads(completion.choices[0].message.content))
    parts = str(data.get("part_numbers_or_specs") or "").strip()
    if parts.lower() in {"", "nan", "none", "null"}:
        data["part_numbers_or_specs"] = "غير محدد"
    problem_text = str(data.get("problem_or_topic") or "")
    solution_text = str(data.get("solution_or_summary") or "")
    if is_dealer_or_maintenance_msg(thread_text) and re.search(r"[\u0600-\u06FF]", problem_text + solution_text):
        data["is_worth_archiving"] = True
        if str(data.get("category") or "") in {"", "نبض القروب والدردشة العامة"}:
            data["category"] = "الصيانة الدورية والوكالات"
    elif data.get("is_worth_archiving") and not re.search(r"[\u0600-\u06FF]", problem_text):
        data["is_worth_archiving"] = False
    return data


VISION_MODELS = ["meta-llama/llama-4-scout-17b-16e-instruct"]
WHISPER_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]


def analyze_image_bytes(api_key: str, image_bytes: bytes, caption: str = "", mime_type: str = "image/jpeg") -> str:
    """يحلل صورة الطبلون أو القطعة أو الفاتورة ويعيد وصفاً عربياً."""
    payload = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "حلل هذه الصورة الخاصة بسيارة جايكو (Jaecoo) بدقة باللغة العربية الفصحى فقط. "
        "إذا كانت صورة شاشة أو طبلون، اذكر التنبيه أو العطل الظاهر. "
        "إذا كانت صورة قطعة غيار أو فاتورة، استخرج اسم القطعة ورقمها أو سعرها. "
        f"النص المرفق مع الصورة: {caption}"
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
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{payload}"}},
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