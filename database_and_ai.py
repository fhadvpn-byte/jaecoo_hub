import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

DB_NAME = "jaecoo_knowledge.db"
CONFIG_FILE = "config.json"

# ==========================================
# 1. إدارة الإعدادات الدائمة (Config Manager)
# ==========================================
def load_config() -> dict:
    default_cfg = {
        "gemini_api_key": "",
        "tg_api_id": "",
        "tg_api_hash": "",
        "tg_group": "",
        "auto_reply_enabled": False,
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
# 2. القالب المهيكل للبطاقة الفنية (Schema)
# ==========================================
class KnowledgeCardSchema(BaseModel):
    is_worth_archiving: bool = Field(
        description="True إذا كان النقاش أو الصورة أو الصوت يحتوي على معلومة فنية أو تجربة أو سؤال مفيد. False للتحيات القصيرة والردود الفارغة."
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

    car_model: Literal["Jaecoo J8", "Jaecoo J7", "جميع الطرازات / عام"] = Field(
        description="طراز السيارة"
    )
    sub_system: Literal[
        "المحرك والتيربو",
        "القير والدفع الرباعي AWD",
        "الكهرباء والبطارية والحساسات",
        "الشاشة والنظام الصوتي والربط",
        "التكييف والتبريد",
        "العضلات والفرامل والتعليق",
        "الهيكل الخارجي والمقصورة",
        "عام",
    ] = Field(description="النظام الفرعي للسيارة")

    title: str = Field(description="عنوان واضح ومختصر للبطاقة (5 إلى 10 كلمات)")
    problem_or_topic: str = Field(description="ملخص المشكلة أو الاستفسار أو الموضوع بدقة")
    solution_or_summary: str = Field(description="الحل التفصيلي المستخلص من الردود أو الصوت أو الصورة")
    part_numbers_or_specs: Optional[str] = Field(
        default="",
        description="أرقام قطع الغيار (Part Numbers)، المقاسات، لزوجة الزيت، التكلفة، أو أسماء الأجهزة والتطبيقات",
    )
    is_verified_solution: bool = Field(
        description="True إذا تضمن النقاش حلاً مجرباً ومؤكداً"
    )
    keywords: str = Field(description="كلمات مفتاحية مفصولة بفاصلة")


SYSTEM_PROMPT = """
أنت كبير مهندسي صيانة وتشخيص سيارات جايكو (Jaecoo J7 و Jaecoo J8) ومحلل بيانات القروب.
مهمتك قراءة نقاشات الأعضاء (نصوص، سلاسل ردود، صور قطع غيار/فواتير/شاشات أعطال، أو بصمات صوتية) وتحويلها إلى بطاقة معرفة فنية.

قواعد العمل:
1. ادمج السؤال مع جميع الردود التابعة له لاستخلاص المشكلة من جهة والحل النهائي من جهة أخرى.
2. إذا أُرفقت صورة (كرتون قطعة غيار، فاتورة صيانة، شاشة طبلون، إكسسوار)، استخرج رقم القطعة أو الكود أو السعر بدقة وضعها في حقل المواصفات.
3. إذا أُرفق ملف صوتي، فرّغ محتواه الفني واستخلص منه التشخيص أو الحل مباشرة.
4. صنّف البطاقة بدقة في أحد الأقسام الثمانية المعتمدة.
"""

# ==========================================
# 3. إدارة قاعدة البيانات (SQLite + WAL)
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=20, check_same_thread=False)
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
# 4. دوال الذكاء الاصطناعي (تحليل + وسائط + بحث + ملخص)
# ==========================================
def analyze_thread_with_ai(
    thread_text: str,
    api_key: str,
    media_bytes: bytes = None,
    mime_type: str = None,
) -> dict:
    client = genai.Client(api_key=api_key)
    prompt = f"حلل المحتوى التالي من قروب ملاك جايكو واستخرج البطاقة الفنية المهيكلة:\n\n{thread_text}"
    contents = [prompt]

    if media_bytes and mime_type:
        contents.append(types.Part.from_bytes(data=media_bytes, mime_type=mime_type))

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=KnowledgeCardSchema,
            temperature=0.1,
        ),
    )
    return json.loads(response.text)


def find_matching_solution_in_db(question_text: str, api_key: str) -> Optional[str]:
    """تُستخدم للرد التلقائي في التلجرام أو من داخل المنصة إذا تكرر سؤال له حل معتمد"""
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

    client = genai.Client(api_key=api_key)
    res = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f"الأرشيف المعتمد لقروب جايكو:\n{archive_context}\n\n"
            f"سؤال العضو الحالي:\n{question_text}\n\n"
            "إذا كان سؤال العضو له حل واضح في الأرشيف أعلاه، اكتب إجابة مختصرة وودودة تبدأ بـ "
            "'💠 أهلاً بك! تمت مناقشة هذا الموضوع سابقاً في القروب وإليك الخلاصة المعتمدة:' متبوعة بالحل واسم صاحب التجربة. "
            "أما إذا لم يكن السؤال موجوداً في الأرشيف، فاكتب كلمة NO_MATCH فقط."
        ),
        config=types.GenerateContentConfig(temperature=0.1),
    )
    reply = res.text.strip()
    return None if "NO_MATCH" in reply else reply


def generate_daily_pulse_summary(api_key: str) -> str:
    """توليد نشرة ملخص نبض القروب من آخر البطاقات المسجلة"""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT category, car_model, title, solution_or_summary, author_name FROM knowledge_cards ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()

    if not rows:
        return "لا توجد بيانات كافية لتوليد الملخص."

    items = "\n".join([f"- ({r[0]} | {r[1]}) {r[2]}: {r[3]} [بواسطة: {r[4]}]" for r in rows])
    client = genai.Client(api_key=api_key)
    res = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f"بناءً على أحدث المواضيع الموثقة في قروب جايكو:\n{items}\n\n"
            "صِغ نشرة موجزة ومنظمة بعنوان '⚡ الموجز الذكي لنبض قروب جايكو' تصلح للنشر في القروب أو القراءة السريعة، "
            "تبرز أهم الحلول المكتشفة، القطع البديلة، وأبرز النقاشات في نقاط أنيقة."
        ),
    )
    return res.text
