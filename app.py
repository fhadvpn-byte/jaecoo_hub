import html
import os
import subprocess
import sys

import pandas as pd
import streamlit as st
from google import genai

from database_and_ai import (
    analyze_thread_with_ai,
    generate_daily_pulse_summary,
    get_db_connection,
    init_db,
    load_config,
    save_card,
    save_config,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))

st.set_page_config(
    page_title="منصة جايكو الذكية | هوية حسم المضيئة",
    page_icon="💠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==========================================
# 1. ثيم "حسم" الأزرق النيون والزجاج الشفاف (CSS)
# ==========================================
st.html("""
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

    div[data-testid="stSidebar"] {
        direction: rtl;
        text-align: right;
        background: rgba(7, 16, 30, 0.88) !important;
        backdrop-filter: blur(20px) !important;
        border-left: 1px solid rgba(56, 189, 248, 0.3) !important;
        box-shadow: -5px 0 25px rgba(0, 195, 255, 0.08) !important;
    }

    .hasm-header {
        background: rgba(13, 27, 48, 0.65);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
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
    .hasm-header::before {
        content: "";
        position: absolute;
        top: 0; right: 0; left: 0;
        height: 3px;
        background: linear-gradient(90deg, transparent, #00f2fe, #38bdf8, transparent);
        box-shadow: 0 0 15px #00f2fe;
    }
    .hasm-title {
        margin: 0;
        font-size: 1.9rem;
        font-weight: 800;
        color: #ffffff;
        text-shadow: 0 0 18px rgba(56, 189, 248, 0.6);
    }
    .hasm-subtitle-en {
        display: inline-block;
        direction: ltr;
        font-family: monospace;
        font-size: 0.88rem;
        letter-spacing: 1.5px;
        color: #38bdf8;
        background: rgba(14, 165, 233, 0.12);
        border: 1px solid rgba(56, 189, 248, 0.35);
        padding: 3px 12px;
        border-radius: 20px;
        margin-top: 8px;
        box-shadow: 0 0 12px rgba(56, 189, 248, 0.2);
    }

    div[data-testid="stMetric"] {
        background: rgba(13, 27, 48, 0.65) !important;
        backdrop-filter: blur(16px) !important;
        border: 1px solid rgba(56, 189, 248, 0.28) !important;
        border-radius: 14px !important;
        padding: 16px 20px !important;
        box-shadow:
            0 8px 32px 0 rgba(0, 0, 0, 0.37),
            inset 0 0 15px rgba(56, 189, 248, 0.06) !important;
        transition: all 0.3s ease !important;
    }
    div[data-testid="stMetric"]:hover {
        border-color: rgba(0, 242, 254, 0.7) !important;
        box-shadow:
            0 8px 32px 0 rgba(0, 0, 0, 0.5),
            0 0 20px rgba(56, 189, 248, 0.3),
            inset 0 0 18px rgba(56, 189, 248, 0.12) !important;
        transform: translateY(-2px);
    }
    div[data-testid="stMetricValue"] {
        color: #38bdf8 !important;
        text-shadow: 0 0 14px rgba(56, 189, 248, 0.5) !important;
        font-weight: 800 !important;
    }
    div[data-testid="stMetricLabel"] {
        color: #cbd5e1 !important;
        font-weight: 600 !important;
    }

    .hasm-card {
        background: rgba(13, 27, 48, 0.65);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border: 1px solid rgba(56, 189, 248, 0.28);
        border-right: 4px solid #00f2fe;
        border-radius: 14px;
        padding: 20px 22px;
        margin-bottom: 12px;
        box-shadow:
            0 8px 32px 0 rgba(0, 0, 0, 0.4),
            inset 0 0 15px rgba(56, 189, 248, 0.05);
        transition: all 0.3s ease;
    }
    .hasm-card:hover {
        border-color: rgba(56, 189, 248, 0.65);
        box-shadow:
            0 10px 35px rgba(0, 0, 0, 0.55),
            0 0 22px rgba(0, 242, 254, 0.22),
            inset 0 0 20px rgba(56, 189, 248, 0.1);
    }

    .badge-neon-cyan {
        background: rgba(0, 242, 254, 0.12);
        color: #67e8f9;
        border: 1px solid rgba(0, 242, 254, 0.45);
        box-shadow: 0 0 10px rgba(0, 242, 254, 0.25);
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: 700;
        display: inline-block;
        margin-left: 8px;
    }
    .badge-neon-blue {
        background: rgba(14, 165, 233, 0.15);
        color: #7dd3fc;
        border: 1px solid rgba(56, 189, 248, 0.35);
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.8rem;
        display: inline-block;
        margin-left: 8px;
    }
    .badge-soft-slate {
        background: rgba(30, 41, 59, 0.75);
        color: #cbd5e1;
        border: 1px solid rgba(148, 163, 184, 0.25);
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.8rem;
        display: inline-block;
        margin-left: 8px;
    }

    .part-spec-box {
        background: rgba(7, 17, 34, 0.75);
        border: 1px solid rgba(56, 189, 248, 0.3);
        border-right: 3px solid #38bdf8;
        padding: 10px 14px;
        border-radius: 10px;
        margin-top: 12px;
        color: #bae6fd;
        box-shadow: inset 0 0 12px rgba(56, 189, 248, 0.08);
    }
    .radar-bar-bg {
        background: rgba(15, 23, 42, 0.8);
        border-radius: 10px;
        height: 12px;
        width: 100%;
        overflow: hidden;
        border: 1px solid rgba(56, 189, 248, 0.2);
        margin-top: 6px;
        margin-bottom: 14px;
    }
    .radar-bar-fill {
        background: linear-gradient(90deg, #0284c7, #00f2fe);
        height: 100%;
        border-radius: 10px;
        box-shadow: 0 0 12px #00f2fe;
    }
    .author-row {
        padding: 10px 14px;
        margin-bottom: 8px;
        background: rgba(7, 17, 34, 0.65);
        border: 1px solid rgba(56, 189, 248, 0.25);
        border-radius: 10px;
        display: flex;
        justify-content: space-between;
        gap: 8px;
        align-items: center;
    }

    .stTextInput > div > div, .stSelectbox > div > div, .stTextArea > div > div {
        background: rgba(10, 22, 40, 0.75) !important;
        border: 1px solid rgba(56, 189, 248, 0.3) !important;
        border-radius: 10px !important;
        color: #f8fafc !important;
        box-shadow: inset 0 0 10px rgba(56, 189, 248, 0.05) !important;
    }
    .stTextInput > div > div:focus-within, .stSelectbox > div > div:focus-within, .stTextArea > div > div:focus-within {
        border-color: #00f2fe !important;
        box-shadow: 0 0 15px rgba(0, 242, 254, 0.35) !important;
    }
    div[data-testid="stExpander"] {
        background: rgba(13, 27, 48, 0.6) !important;
        backdrop-filter: blur(14px) !important;
        border: 1px solid rgba(56, 189, 248, 0.28) !important;
        border-radius: 12px !important;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3) !important;
    }

    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: linear-gradient(135deg, rgba(14, 165, 233, 0.22) 0%, rgba(3, 105, 161, 0.35) 100%) !important;
        color: #e0f2fe !important;
        border: 1px solid rgba(56, 189, 248, 0.45) !important;
        border-radius: 10px !important;
        font-weight: 700 !important;
        box-shadow: 0 0 14px rgba(14, 165, 233, 0.18) !important;
        transition: all 0.25s ease !important;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover {
        background: linear-gradient(135deg, rgba(0, 242, 254, 0.35) 0%, rgba(14, 165, 233, 0.5) 100%) !important;
        border-color: #00f2fe !important;
        color: #ffffff !important;
        box-shadow: 0 0 22px rgba(0, 242, 254, 0.45) !important;
        transform: translateY(-1px);
    }

    div.stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background: rgba(9, 20, 38, 0.65);
        padding: 8px;
        border-radius: 14px;
        border: 1px solid rgba(56, 189, 248, 0.22);
        flex-wrap: wrap;
    }
    div.stTabs [data-baseweb="tab"] {
        background: rgba(13, 27, 48, 0.5);
        border: 1px solid rgba(56, 189, 248, 0.18);
        border-radius: 10px;
        padding: 8px 14px;
        color: #94a3b8;
        font-weight: 600;
        transition: all 0.25s ease;
    }
    div.stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, rgba(14, 165, 233, 0.3) 0%, rgba(2, 132, 199, 0.45) 100%) !important;
        color: #ffffff !important;
        border: 1px solid #00f2fe !important;
        box-shadow: 0 0 16px rgba(0, 242, 254, 0.35) !important;
    }
</style>
""")

# ==========================================
# 2. دوال قاعدة البيانات والبيانات الأولية
# ==========================================
def load_cards():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM knowledge_cards ORDER BY id DESC", conn)
    conn.close()
    return df


def delete_card_by_id(card_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM knowledge_cards WHERE id = ?", (int(card_id),))
    conn.commit()
    conn.close()


def toggle_verify_card(card_id: int, current_state: int):
    new_state = 0 if int(current_state) == 1 else 1
    conn = get_db_connection()
    conn.execute(
        "UPDATE knowledge_cards SET is_verified_solution = ? WHERE id = ?",
        (new_state, int(card_id)),
    )
    conn.commit()
    conn.close()


def next_message_id() -> int:
    conn = get_db_connection()
    row = conn.execute("SELECT COALESCE(MAX(telegram_msg_id), 2000) FROM knowledge_cards").fetchone()
    conn.close()
    return int(row[0]) + 1


def seed_demo_data():
    demo_cards = [
        {
            "id": 1001, "url": "https://t.me/jaecoo_group/1001", "author": "أبو إياد",
            "card": {
                "category": "الأنظمة الذكية والشاشات",
                "car_model": "Jaecoo J8",
                "sub_system": "الشاشة والنظام الصوتي والربط",
                "title": "حل تأخر اتصال Carlinkit Tbox مع شاشة الجايكو",
                "problem_or_topic": "عند تشغيل السيارة يتأخر الاتصال التلقائي بالقطعة أو يفصل البلوتوث المدمج عند ربط الجوال.",
                "solution_or_summary": "ضبط أتمتة عبر تطبيق MacroDroid في الجوال لتفعيل نقطة الاتصال والبلوتوث فور الاتصال بشاشة السيارة مع تثبيت تحديث القطعة الأخير.",
                "part_numbers_or_specs": "Carlinkit Tbox Ambient + MacroDroid",
                "is_verified_solution": True,
                "keywords": "شاشة، كارلنكت، أندرويد أوتو، اتصال تلقائي",
            },
            "raw": "نقاش حول أفضل إعدادات ربط قطعة الأندرويد الخارجية في جايكو J8 دون تعليق.",
        },
        {
            "id": 1002, "url": "https://t.me/jaecoo_group/1002", "author": "سلمان",
            "card": {
                "category": "بنك القطع البديلة والمطابقات",
                "car_model": "Jaecoo J8",
                "sub_system": "الكهرباء والبطارية والحساسات",
                "title": "ترقية البطارية إلى فئة AGM لدعم الأنظمة الذكية",
                "problem_or_topic": "البحث عن بطارية تتحمل استهلاك الشاشات والداش كام ونظام Start-Stop بكفاءة عالية في الأجواء الحارة.",
                "solution_or_summary": "تركيب بطارية AGM بنفس مقاس القاعدة والأمبير الأصلي، تعطي ثباتاً ممتازاً للفولتية وتمنع ظهور رسائل ضعف البطارية.",
                "part_numbers_or_specs": "بطارية AGM مقاس H6 / 70Ah",
                "is_verified_solution": True,
                "keywords": "بطارية، AGM، كهرباء، فولتية",
            },
            "raw": "تجربة استبدال البطارية ببطارية AGM وملاحظة تحسن استقرار الكهرباء.",
        },
        {
            "id": 1003, "url": "https://t.me/jaecoo_group/1003", "author": "خالد الحربي",
            "card": {
                "category": "الإصلاحات والحلول المؤكدة",
                "car_model": "Jaecoo J8",
                "sub_system": "المحرك والتيربو",
                "title": "حل مشكلة عدم استجابة التشغيل عن بُعد من الريموت والتطبيق",
                "problem_or_topic": "التشغيل عن بعد لا يعمل أحياناً ويعطي فشل في الأمر رغم إقفال الأبواب.",
                "solution_or_summary": "التأكد من تفعيل خيار تهيئة التكييف في إعدادات الشاشة، وإغلاق القفل مرتين متتاليتين قبل الضغط المطول، وتحديث وحدة الـ T-Box في الوكالة.",
                "part_numbers_or_specs": "تحديث برمجي لوحدة T-Box لدى الوكيل",
                "is_verified_solution": True,
                "keywords": "تشغيل عن بعد، ريموت، تطبيق، برمجة",
            },
            "raw": "سلسلة ردود حول طريقة تفعيل وحل مشكلة التشغيل عن بعد في J8.",
        },
        {
            "id": 1004, "url": "https://t.me/jaecoo_group/1004", "author": "أبو مالك",
            "card": {
                "category": "تجارب القيادة والأداء الفعلي",
                "car_model": "Jaecoo J8",
                "sub_system": "القير والدفع الرباعي AWD",
                "title": "تجربة ثبات الدفع الرباعي المستمر AWD في العقبات والأمطار",
                "problem_or_topic": "تقييم أداء السيارة في المنعطفات الجبلية الحادة والطرق المبتلة وعزم المحرك في الصعود.",
                "solution_or_summary": "توزيع العزم في نظام AWD ممتاز جداً في العقبات والمنعطفات مع استجابة سريعة للقير عند وضع Sport، وحرارة المحرك والقير ثابتة تماماً.",
                "part_numbers_or_specs": "نظام الدفع الرباعي الذكي ARDIS / AWD",
                "is_verified_solution": True,
                "keywords": "عقبة، مرتفعات، دفع رباعي، رساوة، حرارة",
            },
            "raw": "تقرير مفصل عن قيادة جايكو J8 في الطرق الجبلية والعقبات.",
        },
        {
            "id": 1005, "url": "https://t.me/jaecoo_group/1005", "author": "أبو إياد",
            "card": {
                "category": "الإكسسوارات والتجهيزات",
                "car_model": "Jaecoo J8",
                "sub_system": "الهيكل الخارجي والمقصورة",
                "title": "حامل جوال مخصص مع شاحن سريع 66W وحماية الكونسول",
                "problem_or_topic": "البحث عن حامل جوال يركب بدون لصق ولا يحجب الشاشة مع بطانة سيليكون لحماية منطقة الأكواب والكونسول.",
                "solution_or_summary": "تم تجربة القاعدة المخصصة لموديل J8 بقدرة شحن 66 واط مع حصيرة السيليكون المطابقة لمقاس الكونسول الوسطي وتمنع أصوات الاهتزاز.",
                "part_numbers_or_specs": "قاعدة شحن 66W مخصصة + بطانة سيليكون للكونسول",
                "is_verified_solution": True,
                "keywords": "إكسسوارات، حامل جوال، شاحن، كونسول",
            },
            "raw": "مشاركة صور وروابط إكسسوارات داخلية مجربة للجايكو.",
        },
        {
            "id": 1006, "url": "https://t.me/jaecoo_group/1006", "author": "ماجد",
            "card": {
                "category": "الأعطال والتشخيص",
                "car_model": "Jaecoo J7",
                "sub_system": "العضلات والفرامل والتعليق",
                "title": "صوت طقطقة خفيف في الجهة الأمامية عند المطبات القاسية",
                "problem_or_topic": "ظهور صوت خفيف من أسفل السيارة عند النزول من مطب بسرعة منخفضة.",
                "solution_or_summary": "جاري التشخيص بين جلد عمود التوازن أو كراسي المساعدات، وينصح بشد مسامير النظام السفلي في صيانة الـ 10 آلاف كم.",
                "part_numbers_or_specs": "فحص جلد عمود التوازن (Sway Bar Bushings)",
                "is_verified_solution": False,
                "keywords": "صوت، مطبات، مساعدات، عضلات",
            },
            "raw": "استفسار من أحد الأعضاء عن صوت في المطبات والردود المقترحة للفحص.",
        },
    ]
    for item in demo_cards:
        save_card(item["card"], item["id"], item["url"], item["author"], item["raw"])


def config_ready(cfg: dict) -> bool:
    return bool(cfg.get("tg_api_id") and cfg.get("tg_api_hash") and cfg.get("tg_group") and cfg.get("gemini_api_key"))


def radar_systems_html(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "<div class='hasm-card'><h4>📡 رادار الأنظمة الأكثر نقاشاً</h4><p>لا توجد بطاقات بعد.</p></div>"
    sys_counts = frame["sub_system"].value_counts()
    max_val = int(sys_counts.max()) or 1
    bars = []
    for sys_name, cnt in sys_counts.items():
        pct = int((int(cnt) / max_val) * 100)
        bars.append(
            f"""
            <div style="display:flex; justify-content:space-between; font-size:0.9rem; color:#bae6fd; gap:8px;">
                <span>⚙️ {html.escape(str(sys_name))}</span>
                <b>{int(cnt)} بطاقة</b>
            </div>
            <div class="radar-bar-bg"><div class="radar-bar-fill" style="width:{pct}%;"></div></div>
            """
        )
    return "<div class='hasm-card'><h4>📡 رادار الأنظمة الأكثر نقاشاً</h4>" + "".join(bars) + "</div>"


def top_authors_html(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "<div class='hasm-card'><h4>🏆 نخبة القروب (الأكثر إثراءً للمعرفة)</h4><p>لا توجد بطاقات بعد.</p></div>"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    rows = []
    for rank, (author, cnt) in enumerate(frame["author_name"].value_counts().head(6).items(), 1):
        medal = medals.get(rank, "💠")
        rows.append(
            f"""
            <div class="author-row">
                <span>{medal} <b style="color:#f8fafc;">{html.escape(str(author))}</b></span>
                <span class="badge-neon-cyan">{int(cnt)} مساهمة موثقة</span>
            </div>
            """
        )
    return "<div class='hasm-card'><h4>🏆 نخبة القروب (الأكثر إثراءً للمعرفة)</h4>" + "".join(rows) + "</div>"


init_db()
cfg = load_config()
if "initial_seeded" not in st.session_state:
    if load_cards().empty:
        seed_demo_data()
    st.session_state["initial_seeded"] = True

# ==========================================
# 3. القائمة الجانبية (إعدادات دائمة + وسائط + تلجرام)
# ==========================================
CATEGORIES_LIST = [
    "الأعطال والتشخيص",
    "الإصلاحات والحلول المؤكدة",
    "بنك القطع البديلة والمطابقات",
    "الأنظمة الذكية والشاشات",
    "الصيانة الدورية والوكالات",
    "الإكسسوارات والتجهيزات",
    "تجارب القيادة والأداء الفعلي",
    "نبض القروب والدردشة العامة",
]
SYSTEMS_LIST = [
    "المحرك والتيربو",
    "القير والدفع الرباعي AWD",
    "الكهرباء والبطارية والحساسات",
    "الشاشة والنظام الصوتي والربط",
    "التكييف والتبريد",
    "العضلات والفرامل والتعليق",
    "الهيكل الخارجي والمقصورة",
    "عام",
]

with st.sidebar:
    st.html("<h3 style='color:#38bdf8; text-shadow:0 0 12px rgba(56,189,248,0.5); margin:0;'>💠 مركز التحكم والربط</h3>")

    with st.expander("🔐 إعدادات المفاتيح والربط بالتلجرام (حفظ دائم)", expanded=not bool(cfg.get("gemini_api_key"))):
        new_gemini = st.text_input("مفتاح Gemini API:", value=cfg.get("gemini_api_key", ""), type="password")
        new_api_id = st.text_input("Telegram API ID:", value=cfg.get("tg_api_id", ""))
        new_api_hash = st.text_input("Telegram API Hash:", value=cfg.get("tg_api_hash", ""), type="password")
        new_group = st.text_input("معرف أو رابط قروب الجايكو:", value=cfg.get("tg_group", ""), placeholder="jaecoo_ksa_group")
        new_auto_reply = st.checkbox("تفعيل الرد الآلي على الأسئلة المتكررة 🤖", value=bool(cfg.get("auto_reply_enabled", False)))

        if st.button("حفظ الإعدادات بشكل دائم 💾", width="stretch"):
            cfg.update({
                "gemini_api_key": new_gemini.strip(),
                "tg_api_id": new_api_id.strip(),
                "tg_api_hash": new_api_hash.strip(),
                "tg_group": new_group.strip(),
                "auto_reply_enabled": new_auto_reply,
            })
            save_config(cfg)
            st.toast("تم حفظ الإعدادات في config.json")
            st.rerun()

    gemini_key = cfg.get("gemini_api_key", "")

    with st.expander("📡 سحب ومزامنة قروب التلجرام"):
        pull_count = st.slider("عدد الرسائل المراد سحبها وتحليلها:", 20, 500, 100, step=20)
        if st.button("بدء سحب وتصنيف رسائل القروب ⚡", width="stretch"):
            if not config_ready(cfg):
                st.warning("أكمل بيانات التلجرام ومفتاح Gemini في الأعلى أولاً.")
            else:
                with st.spinner("جاري الاتصال بتلجرام ودمج الردود وتحليلها..."):
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    try:
                        res = subprocess.run(
                            [sys.executable, os.path.join(APP_DIR, "telegram_scraper.py"), "--mode", "history", "--limit", str(pull_count)],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            cwd=APP_DIR,
                            env=env,
                            timeout=180,
                        )
                    except subprocess.TimeoutExpired:
                        st.error("انتهت مهلة الاتصال. أول تسجيل دخول يحتاج رقم الجوال والرمز في الطرفية.")
                        st.code("python telegram_scraper.py --mode history --limit 100")
                    else:
                        output = ((res.stdout or "") + ("\n" + res.stderr if res.stderr else "")).strip()
                        if res.returncode == 0:
                            st.toast("اكتملت المزامنة")
                            if output:
                                st.code(output[-400:])
                            st.rerun()
                        else:
                            st.error("حدثت ملاحظة أثناء الاتصال. أول تسجيل دخول يتم من الطرفية.")
                            st.code(output or "لا توجد تفاصيل")

    with st.expander("🎙️📷 مختبر التحليل الذكي (نص / صورة / بصمة صوتية)"):
        sample_chat = st.text_area(
            "الصق نقاشاً أو اكتب وصفاً مرفقاً بصورة أو صوت:",
            placeholder="مثال: صورة كرتون بواجي أو فلتر يركب على جايكو J8 مع تعليق العضو...",
        )
        uploaded_media = st.file_uploader(
            "إرفاق صورة (قطعة/فاتورة/عطل) أو بصمة صوتية (اختياري):",
            type=["png", "jpg", "jpeg", "ogg", "mp3", "wav", "m4a"],
        )
        if st.button("تحليل وإضافة للقسم المناسب ✨", width="stretch"):
            if not gemini_key:
                st.info("أدخل مفتاح Gemini API واحفظه أولاً.")
            elif not sample_chat.strip() and not uploaded_media:
                st.info("أدخل نصاً أو ارفع ملف صورة/صوت للتحليل.")
            else:
                try:
                    with st.spinner("جاري تحليل المحتوى والوسائط بالذكاء الاصطناعي..."):
                        m_bytes = uploaded_media.read() if uploaded_media else None
                        m_mime = uploaded_media.type if uploaded_media else None
                        res = analyze_thread_with_ai(sample_chat or "تحليل مرفق وسائط فني", gemini_key, m_bytes, m_mime)
                        if not res.get("is_worth_archiving", True):
                            st.info("المحتوى لا يحتوي معلومة تستحق الأرشفة.")
                        else:
                            save_card(res, next_message_id(), "#", "تحليل ذكي متعدد الوسائط", sample_chat)
                            st.toast(f"تم التصنيف في: {res['category']}")
                            st.rerun()
                except Exception as exc:
                    st.error(f"خطأ في التحليل: {exc}")

    with st.expander("➕ إضافة بطاقة معرفة يدوياً"):
        with st.form("manual_card_form", clear_on_submit=True):
            m_title = st.text_input("عنوان البطاقة:")
            m_cat = st.selectbox("القسم:", CATEGORIES_LIST)
            m_model = st.selectbox("الطراز:", ["Jaecoo J8", "Jaecoo J7", "جميع الطرازات / عام"])
            m_sys = st.selectbox("النظام الفرعي:", SYSTEMS_LIST)
            m_prob = st.text_area("المشكلة / الموضوع:")
            m_sol = st.text_area("الحل / الخلاصة:")
            m_parts = st.text_input("أرقام القطع أو المواصفات:")
            m_author = st.text_input("اسم المشارك:", value="أبو إياد")
            m_ver = st.checkbox("حل مجرب ومعتمد 🛡️", value=True)

            submitted = st.form_submit_button("حفظ البطاقة ⚡", width="stretch")
            if submitted and m_title.strip():
                manual_data = {
                    "category": m_cat,
                    "car_model": m_model,
                    "sub_system": m_sys,
                    "title": m_title.strip(),
                    "problem_or_topic": m_prob.strip() or "بدون تفاصيل إضافية",
                    "solution_or_summary": m_sol.strip() or "بدون تفاصيل إضافية",
                    "part_numbers_or_specs": m_parts.strip(),
                    "is_verified_solution": m_ver,
                    "keywords": f"{m_cat}، {m_sys}، {m_model}",
                }
                save_card(manual_data, next_message_id(), "#", m_author, m_prob)
                st.toast("تم حفظ البطاقة")
                st.rerun()
            elif submitted:
                st.warning("اكتب عنوان البطاقة قبل الحفظ.")

    st.divider()
    if st.button("🔄 استعادة البيانات النموذجية", width="stretch"):
        seed_demo_data()
        st.rerun()

# ==========================================
# 4. الترويسة المضيئة والعدادات
# ==========================================
df = load_cards()
status_badge = (
    '<span class="badge-neon-cyan">⚡ متصل بالذكاء الاصطناعي</span>'
    if gemini_key
    else '<span class="badge-soft-slate">🔒 وضع التصفح المحلي (أضف مفتاح Gemini للتوليد)</span>'
)
st.html(f"""
<div class="hasm-header">
    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
        <div>
            <h1 class="hasm-title">💠 منصة جايكو الذكية</h1>
            <div class="hasm-subtitle-en">JAECOO AI KNOWLEDGE &amp; DIAGNOSTICS HUB</div>
        </div>
        <div>{status_badge}</div>
    </div>
    <p style="margin: 12px 0 0 0; color: #94a3b8; font-size: 0.95rem;">
        منظومة ذكاء اصطناعي لتحليل نقاشات قروب التلجرام وتصنيف الأعطال، الحلول المؤكدة، القطع البديلة، والأنظمة الذكية
    </p>
</div>
""")

m1, m2, m3, m4 = st.columns(4)
m1.metric("💠 إجمالي البطاقات", len(df))
m2.metric("🛡️ الحلول المعتمدة", int(df["is_verified_solution"].sum()) if not df.empty else 0)
m3.metric("🔹 بطاقات Jaecoo J8", len(df[df["car_model"] == "Jaecoo J8"]) if not df.empty else 0)
m4.metric("🌐 الأقسام المغطاة", df["category"].nunique() if not df.empty else 0)

# ==========================================
# 5. شريط البحث والفلاتر والمهندس الذكي
# ==========================================
r1_c1, r1_c2 = st.columns([2.3, 1], vertical_alignment="bottom")
with r1_c1:
    search_query = st.text_input(
        "🔍 البحث الفوري في قاعدة المعرفة:",
        placeholder="ابحث عن: بطارية AGM، شاشة، تشغيل عن بعد، عقبة، رقم قطعة...",
    )
with r1_c2:
    only_verified = st.checkbox("🛡️ إظهار الحلول المعتمدة فقط")

r2_c1, r2_c2 = st.columns(2)
with r2_c1:
    model_filter = st.selectbox("🚘 تصفية حسب الطراز:", ["الكل", "Jaecoo J8", "Jaecoo J7", "جميع الطرازات / عام"])
with r2_c2:
    system_filter = st.selectbox("⚙️ تصفية حسب النظام الفرعي:", ["الكل"] + SYSTEMS_LIST)

with st.expander("📡 استشارة المهندس الذكي (بحث سياقي RAG في تجارب القروب)"):
    ai_q = st.text_input(
        "اطرح سؤالك الفني ليستخلص الذكاء الاصطناعي الإجابة من البطاقات:",
        placeholder="مثال: كيف أحل مشكلة تأخر اتصال الشاشة أو التشغيل عن بعد؟",
    )
    if st.button("استخلاص الإجابة الفورية ⚡"):
        if not gemini_key:
            st.info("أدخل مفتاح Gemini API في القائمة الجانبية لتفعيل الاستشارة الذكية.")
        elif not ai_q.strip():
            st.info("اكتب السؤال أولاً.")
        elif df.empty:
            st.info("لا توجد بطاقات مسجلة حالياً.")
        else:
            context_records = "\n---\n".join(
                [
                    f"العنوان: {row['title']} | الطراز: {row['car_model']} | المشكلة: {row['problem_or_topic']} | الحل: {row['solution_or_summary']} | المواصفات: {row['part_numbers_or_specs']} | عضو: {row['author_name']}"
                    for _, row in df.iterrows()
                ]
            )
            try:
                client = genai.Client(api_key=gemini_key)
                ans = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=(
                        f"بناءً على تجارب أعضاء قروب جايكو التالية فقط:\n{context_records}\n\n"
                        f"أجب بدقة ووضوح على سؤال العضو التالي مع ذكر اسم صاحب التجربة:\n{ai_q}"
                    ),
                )
                st.info(ans.text)
            except Exception as exc:
                st.error(f"تعذر استخلاص الإجابة: {exc}")

filtered_df = df.copy()
if not filtered_df.empty:
    if model_filter != "الكل":
        filtered_df = filtered_df[filtered_df["car_model"] == model_filter]
    if system_filter != "الكل":
        filtered_df = filtered_df[filtered_df["sub_system"] == system_filter]
    if only_verified:
        filtered_df = filtered_df[filtered_df["is_verified_solution"] == 1]
    if search_query.strip():
        q = search_query.strip()
        filtered_df = filtered_df[
            filtered_df["title"].str.contains(q, case=False, na=False)
            | filtered_df["problem_or_topic"].str.contains(q, case=False, na=False)
            | filtered_df["solution_or_summary"].str.contains(q, case=False, na=False)
            | filtered_df["part_numbers_or_specs"].str.contains(q, case=False, na=False)
            | filtered_df["keywords"].str.contains(q, case=False, na=False)
        ]

# ==========================================
# 6. الأقسام الثمانية + تبويب الرادار
# ==========================================
TABS_CONFIG = [
    ("💠 الكل", "ALL"),
    ("⚡ الأعطال والتشخيص", "الأعطال والتشخيص"),
    ("🛡️ الحلول المؤكدة", "الإصلاحات والحلول المؤكدة"),
    ("⚙️ القطع البديلة", "بنك القطع البديلة والمطابقات"),
    ("🖥️ الشاشات والأنظمة", "الأنظمة الذكية والشاشات"),
    ("📋 الصيانة والوكالات", "الصيانة الدورية والوكالات"),
    ("✨ الإكسسوارات", "الإكسسوارات والتجهيزات"),
    ("🏔️ تجارب القيادة", "تجارب القيادة والأداء الفعلي"),
    ("💬 نبض القروب", "نبض القروب والدردشة العامة"),
    ("📊 الرادار ونخبة القروب", "RADAR_ANALYTICS"),
]


def render_card(row, tab_prefix):
    verified_html = (
        '<span class="badge-neon-cyan">🛡️ حل مجرب ومعتمد</span>'
        if row["is_verified_solution"]
        else '<span class="badge-soft-slate">⏳ قيد المتابعة والتشخيص</span>'
    )
    parts = row["part_numbers_or_specs"] or ""
    parts_html = (
        f'<div class="part-spec-box">🔹 <b>القطع والمواصفات الفنية:</b> {html.escape(str(parts))}</div>'
        if parts
        else ""
    )
    st.html(f"""
    <div class="hasm-card">
        <div style="margin-bottom: 12px;">
            <span class="badge-neon-blue">🚘 {html.escape(str(row["car_model"]))}</span>
            <span class="badge-soft-slate">⚙️ {html.escape(str(row["sub_system"]))}</span>
            <span class="badge-soft-slate">📂 {html.escape(str(row["category"]))}</span>
            {verified_html}
        </div>
        <h4 style="margin: 6px 0 10px 0; color: #38bdf8; text-shadow: 0 0 10px rgba(56, 189, 248, 0.35); font-size: 1.2rem;">
            {html.escape(str(row["title"]))}
        </h4>
        <p style="margin: 6px 0; color: #e2e8f0; line-height: 1.6;">
            <b style="color: #7dd3fc;">🔹 الوصف / المشكلة:</b> {html.escape(str(row["problem_or_topic"]))}
        </p>
        <p style="margin: 8px 0; color: #ecfeff; line-height: 1.6;">
            <b style="color: #00f2fe;">⚡ الخلاصة / الحل:</b> {html.escape(str(row["solution_or_summary"]))}
        </p>
        {parts_html}
        <div style="margin-top: 14px; padding-top: 10px; border-top: 1px solid rgba(56, 189, 248, 0.15); font-size: 0.82rem; color: #94a3b8; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
            <span>👤 المصدر: <b style="color: #bae6fd;">{html.escape(str(row["author_name"]))}</b> | 🕒 <span dir="ltr">{html.escape(str(row["created_at"]))}</span></span>
            <span>🏷️ {html.escape(str(row["keywords"] or ""))}</span>
        </div>
    </div>
    """)

    btn_c1, btn_c2, _ = st.columns([1.3, 1, 3.7])
    with btn_c1:
        toggle_label = "إلغاء الاعتماد ↩️" if row["is_verified_solution"] else "اعتماد الحل 🛡️"
        if st.button(toggle_label, key=f"ver_{tab_prefix}_{row['id']}", width="stretch"):
            toggle_verify_card(row["id"], row["is_verified_solution"])
            st.rerun()
    with btn_c2:
        if st.button("حذف البطاقة ✖", key=f"del_{tab_prefix}_{row['id']}", width="stretch"):
            delete_card_by_id(row["id"])
            st.rerun()


tab_titles = []
for label, cat_key in TABS_CONFIG:
    if cat_key == "ALL":
        tab_titles.append(f"{label} ({len(filtered_df)})")
    elif cat_key == "RADAR_ANALYTICS":
        tab_titles.append(label)
    else:
        tab_titles.append(f"{label} ({len(filtered_df[filtered_df['category'] == cat_key])})")

tabs = st.tabs(tab_titles)
for idx, (_, cat_key) in enumerate(TABS_CONFIG):
    with tabs[idx]:
        if cat_key == "RADAR_ANALYTICS":
            col_a, col_b = st.columns(2)
            with col_a:
                st.html(radar_systems_html(df))
            with col_b:
                st.html(top_authors_html(df))
            col_c, col_d = st.columns(2)
            with col_c:
                st.subheader("📰 توليد النشرة اليومية لنبض القروب")
                if st.button("✨ توليد موجز القروب الذكي للنشر", width="stretch"):
                    if not gemini_key:
                        st.info("أدخل مفتاح Gemini API في القائمة الجانبية أولاً.")
                    else:
                        try:
                            with st.spinner("جاري صياغة النشرة الذكية..."):
                                summary_text = generate_daily_pulse_summary(gemini_key)
                            st.text_area("انسخ النشرة التالية وشاركها في قروب التلجرام:", value=summary_text, height=220)
                        except Exception as exc:
                            st.error(f"تعذر توليد النشرة: {exc}")
            with col_d:
                st.subheader("📥 النسخ الاحتياطي وتصدير البيانات")
                if df.empty:
                    st.info("لا توجد بطاقات للتصدير.")
                else:
                    csv_data = df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        label="📊 تحميل أرشيف القروب بالكامل (CSV)",
                        data=csv_data,
                        file_name="jaecoo_knowledge_archive.csv",
                        mime="text/csv",
                        width="stretch",
                    )
        else:
            cat_df = filtered_df if cat_key == "ALL" else filtered_df[filtered_df["category"] == cat_key]
            if cat_df.empty:
                st.info("لا توجد بطاقات مطابقة للفلاتر المحددة في هذا القسم.")
            else:
                for _, row in cat_df.iterrows():
                    render_card(row, f"t{idx}")
