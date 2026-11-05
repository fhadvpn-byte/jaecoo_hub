import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

DB_NAME = "jaecoo_knowledge.db"

# ==========================================
# 1. قالب المخرجات المهيكل (Pydantic Schema)
# ==========================================
class KnowledgeCardSchema(BaseModel):
    is_worth_archiving: bool = Field(
        description="هل النقاش يحتوي على معلومة مفيدة أو سؤال أو رأي يستحق الأرشفة؟ (False فقط للردود الفارغة مثل: هلا، شكراً، ملصقات فقط)"
    )
    category: Literal[
        "الأعطال والتشخيص",
        "الإصلاحات والحلول المؤكدة",
        "بنك القطع البديلة والمطابقات",
        "الأنظمة الذكية والشاشات",
        "الصيانة الدورية والوكالات",
        "الإكسسوارات والتجهيزات",
        "تجارب القيادة والأداء الفعلي",
        "نبض القروب والدردشة العامة"
    ] = Field(description="القسم الأنسب من الأقسام الثمانية المعتمدة")

    car_model: Literal["Jaecoo J8", "Jaecoo J7", "جميع الطرازات / عام"] = Field(
        description="طراز السيارة المذكور في النقاش"
    )
    sub_system: Literal[
        "المحرك والتيربو",
        "القير والدفع الرباعي AWD",
        "الكهرباء والبطارية والحساسات",
        "الشاشة والنظام الصوتي والربط",
        "التكييف والتبريد",
        "العضلات والفرامل والتعليق",
        "الهيكل الخارجي والمقصورة",
        "عام"
    ] = Field(description="النظام الفرعي للسيارة المتعلق بالموضوع")

    title: str = Field(description="عنوان واضح ومختصر للبطاقة لا يتجاوز 10 كلمات")
    problem_or_topic: str = Field(description="ملخص المشكلة المطروحة أو الموضوع الأساسي بدقة")
    solution_or_summary: str = Field(description="الحل المستخلص من ردود الأعضاء، أو خلاصة التجربة والرأي")
    part_numbers_or_specs: Optional[str] = Field(
        default="",
        description="أرقام قطع الغيار، مقاسات، لزوجة زيت، أو أسماء أجهزة/تطبيقات ذُكرت في النقاش إن وجدت"
    )
    is_verified_solution: bool = Field(
        description="True إذا أكد صاحب المشكلة أو أكثر من عضو خبير أن الحل مجرب وناجح فعلياً"
    )
    keywords: str = Field(description="كلمات مفتاحية مفصولة بفاصلة لتسهيل البحث السريع")

# ==========================================
# 2. البرومبت الهندسي (System Prompt)
# ==========================================
SYSTEM_PROMPT = """
أنت مهندس سيارات خبير ومحلل بيانات متخصص في سيارات جايكو (Jaecoo J7 و Jaecoo J8).
مهمتك هي قراءة سلسلة رسائل (سؤال مع ردوده) مسحوبة من قروب ملاك جايكو في التلجرام، وتحويلها إلى "بطاقة معرفة فنية موحدة".

قواعد التحليل والتصنيف الصارمة:
1. ادمج السياق: قد يبدأ النقاش بسؤال عن عطل، وتأتي الإجابة في الرد الثالث أو الرابع. استخلص المشكلة من البداية والحل النهائي من الردود.
2. توزيع الأقسام الثمانية:
   - "الأعطال والتشخيص": إذا كان النقاش يعرض مشكلة فنية أو كود عطل أو ظاهرة غريبة ولم يصل الأعضاء لحل نهائي مؤكد بعد، أو كان التركيز على تشخيص السبب.
   - "الإصلاحات والحلول المؤكدة": إذا تضمن النقاش خطوات إصلاح واضحة، أو حل مشكلة في الوكالة/ورشة، أو طريقة برمجة/إصلاح يدوي نجحت بالفعل.
   - "بنك القطع البديلة والمطابقات": إذا ذكر النقاش قطعة غيار من سيارة أخرى (مثل تويوتا، هيونداي، شيري...) تركب على الجايكو، أو بطارية متوافقة (مثل AGM)، أو بواجي وفلاتر بديلة مع أرقامها.
   - "الأنظمة الذكية والشاشات": لكل ما يخص شاشة السيارة، أندرويد أوتو، أبل كار بلاي، أجهزة الـ AI Box (مثل Carlinkit)، تحديثات السوفتوير، الداش كام، والأوامر الصوتية.
   - "الصيانة الدورية والوكالات": لتكاليف وفواتير الصيانة الدورية، لزوجة الزيوت، الضمان، وتقييم فروع الوكالة ومراكز الخدمة.
   - "الإكسسوارات والتجهيزات": لروابط وصور الكماليات (فرشات، حمايات، حوامل جوال، مفاتيح، تجهيزات البر والرحلات).
   - "تجارب القيادة والأداء الفعلي": للانطباعات حول العزل، الرساوة، الأداء في العقبات والمرتفعات الجبلية، استهلاك الوقود، وتجربة الدفع الرباعي.
   - "نبض القروب والدردشة العامة": للنقاشات العامة، الاستفسارات الشرائية العامة، أو السواليف المفيدة التي لا تندرج تحت الأقسام الفنية.
3. حافظ على المصطلحات الفنية والأرقام: إذا ذُكر رقم قطعة أو مقاس أو اسم برنامج أو سعر، اكتبه بدقة في الحقل المخصص.
"""

# ==========================================
# 3. إدارة قاعدة البيانات (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
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
        1 if card_data["is_verified_solution"] else 0,
        card_data["keywords"],
        author_name,
        raw_text,
        datetime.now().strftime("%Y-%m-%d %H:%M")
    ))
    conn.commit()
    conn.close()

# ==========================================
# 4. دالة التحليل بالذكاء الاصطناعي (Gemini)
# ==========================================
def analyze_thread_with_ai(thread_text: str, api_key: str, media_bytes: bytes = None, mime_type: str = None) -> dict:
    client = genai.Client(api_key=api_key)

    contents = [f"حلل سلسلة النقاش التالية من قروب جايكو واستخرج البطاقة الفنية:\n\n{thread_text}"]

    # دعم الوسائط المتعددة (بصمة صوتية أو صورة قطعة غيار/فاتورة)
    if media_bytes and mime_type:
        contents.append(types.Part.from_bytes(data=media_bytes, mime_type=mime_type))

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=KnowledgeCardSchema,
            temperature=0.1
        )
    )
    return json.loads(response.text)
