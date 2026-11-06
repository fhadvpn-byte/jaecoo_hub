import sqlite3
import json
import os
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
        description="True فقط لمعلومة فنية واضحة عن جايكو يمكن صياغتها بالعربية الفصحى. False للرسائل العامة والتحيات والأسئلة الإنجليزية والنصوص غير الواضحة."
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
        "المحرك والتيربو والمنظومة الهجين SHS",
        "القير والدفع الرباعي AWD",
        "الكهرباء والبطارية والحساسات",
        "الشاشة والنظام الصوتي والربط",
        "التكييف والتبريد",
        "العضلات والفرامل والتعليق",
        "الهيكل الخارجي والمقصورة",
        "عام",
    ] = Field(description="النظام الفرعي للسيارة")

    title: str = Field(description="عنوان بالعربية الفصحى حصراً، من 5 إلى 10 كلمات. ممنوع الإنجليزية وممنوع القيمة nan.")
    problem_or_topic: str = Field(description="وصف المشكلة بالعربية الفصحى حصراً. ممنوع nan وممنوع ترك الحقل فارغاً عند الحفظ.")
    solution_or_summary: str = Field(description="الحل أو الخلاصة بالعربية الفصحى حصراً. ممنوع nan وممنوع الجمل الإنجليزية.")
    part_numbers_or_specs: Optional[str] = Field(
        default="",
        description="أرقام القطع والمواصفات كما هي، مع شرح عربي مختصر حولها.",
    )
    is_verified_solution: bool = Field(
        description="True إذا تضمن النقاش حلاً مجرباً ومؤكداً"
    )
    keywords: str = Field(description="كلمات مفتاحية عربية مفصولة بفاصلة")


SYSTEM_PROMPT = """
أنت كبير مهندسي صيانة وتشخيص سيارات جايكو (Jaecoo J8, J8 SHS, J5, J7) ومحلل بيانات قروب التيليجرام.
مهمتك استخراج بطاقة معرفة فنية فقط عندما تحتوي الرسالة على عطل أو حل أو قطعة أو إجراء واضح.
اللغة إلزامية: اكتب title و problem_or_topic و solution_or_summary و keywords بالعربية الفصحى حصراً.
ممنوع الإنجليزية في هذه الحقول، وممنوع القيم nan أو None أو null أو النصوص الفارغة.
تجاهل ولا تؤرشف: الرسائل العامة، التحيات، الدردشة، الأسئلة المكتوبة بالإنجليزية، والنصوص غير الواضحة أو الناقصة.
في هذه الحالات اجعل is_worth_archiving = false ولا تخترع عنواناً أو حلاً.
إذا كانت المعلومة الفنية واضحة فصح بالعربية الفصحى، ويُسمح بإبقاء اسم الطراز أو رقم القطعة داخل الجملة العربية.
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
    conn.commit()
    conn.close()

def save_card(card_data: dict, telegram_msg_id: int, thread_url: str, author_name: str, raw_text: str):
    conn = get_db_connection()
    conn.execute("""
        INSERT OR REPLACE INTO knowledge_cards (
            telegram_msg_id, thread_url, category, car_model, sub_system,
            title, problem_or_topic, solution_or_summary, part_numbers_or_specs,
            is_verified_solution, keywords, author_name, raw_thread_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ))
    conn.commit()
    conn.close()

# ==========================================
# 4. محرك Groq الذكي والسريع
# ==========================================
GROQ_MODELS = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "allam-2-7b"]


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


def analyze_thread_with_ai(thread_text: str, api_key: str, media_bytes=None, mime_type=None) -> dict:
    schema_json_str = json.dumps(KnowledgeCardSchema.model_json_schema(), ensure_ascii=False)
    
    prompt = f"""
{SYSTEM_PROMPT}

يرجى تحليل النص التالي واستخراج النتيجة بصيغة JSON مطابقة تماماً لهذا المخطط (Schema):
{schema_json_str}

قبل الإجابة: إن كانت الرسالة عامة أو سؤالاً إنجليزياً أو نصاً غير واضح فاجعل is_worth_archiving = false.
لا تترجم الأسئلة الإنجليزية العامة إلى بطاقة. الحقول النصية بالعربية الفصحى فقط، ومن دون كلمة nan.

النص المراد تحليله:
{thread_text}
"""

    completion = _groq_complete(
        api_key,
        [
            {"role": "system", "content": "أنت مساعد يستخرج JSON فقط. الحقول النصية بالعربية الفصحى حصراً. تجاهل الرسائل العامة والأسئلة الإنجليزية والنصوص غير الواضحة بجعل is_worth_archiving قيمة false. ممنوع إخراج nan."},
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
    )
    
    result_text = completion.choices[0].message.content
    return json.loads(result_text)


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