import html
import os
import subprocess
import sys
import pandas as pd
import streamlit as st
from google import genai

from database_and_ai import (
    analyze_thread_with_ai,
    find_matching_solution_in_db,
    generate_daily_pulse_summary,
    get_db_connection,
    init_db,
    load_config,
    save_card,
    save_config,
)

# ==========================================
# 1. إعدادات الصفحة والتصميم (هوية حسم الذكية)
# ==========================================
st.set_page_config(
    page_title="منصة جايكو الذكية | هوية حسم",
    page_icon="💠",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_db()
cfg = load_config()

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Tajawal', sans-serif !important;
        direction: rtl;
        text-align: right;
    }
    .stApp {
        background: radial-gradient(circle at top right, #0f2942 0%, #071324 60%, #040b16 100%);
        color: #e2f1ff;
    }
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0b1e36 0%, #071426 100%) !important;
        border-left: 1px solid rgba(0, 210, 255, 0.2);
    }
    .hero-box {
        background: linear-gradient(135deg, rgba(14, 42, 71, 0.85) 0%, rgba(7, 22, 41, 0.9) 100%);
        border: 1px solid rgba(0, 212, 255, 0.3);
        border-radius: 18px;
        padding: 24px;
        margin-bottom: 20px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
    }
    .hub-badge {
        display: inline-block;
        background: rgba(0, 212, 255, 0.12);
        color: #00e1ff;
        border: 1px solid rgba(0, 212, 255, 0.35);
        padding: 4px 14px;
        border-radius: 20px;
        font-size: 0.8rem;
        letter-spacing: 1px;
        margin-bottom: 10px;
    }
    .metric-card {
        background: rgba(13, 35, 61, 0.75);
        border: 1px solid rgba(0, 212, 255, 0.25);
        border-radius: 14px;
        padding: 16px;
        text-align: center;
    }
    .metric-val {
        font-size: 2.2rem;
        font-weight: 800;
        color: #00e1ff;
    }
    .k-card,
    .k-card * {
        direction: rtl !important;
        text-align: right !important;
    }
    .k-card {
        background: linear-gradient(145deg, rgba(16, 40, 69, 0.85), rgba(9, 24, 44, 0.92));
        border: 1px solid rgba(0, 212, 255, 0.25);
        border-radius: 16px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
    }
    .k-tags {
        direction: rtl !important;
        text-align: right !important;
    }
    .k-title {
        color: #00e1ff;
        font-size: 1.25rem;
        font-weight: 700;
        margin: 10px 0;
        direction: rtl !important;
        text-align: right !important;
    }
    .k-tag {
        display: inline-block;
        background: rgba(0, 212, 255, 0.15);
        color: #7ce8ff;
        border: 1px solid rgba(0, 212, 255, 0.3);
        border-radius: 8px;
        padding: 3px 10px;
        font-size: 0.82rem;
        margin-left: 6px;
        direction: rtl !important;
        text-align: right !important;
    }
    .k-section {
        background: rgba(6, 17, 33, 0.65);
        border-right: 3px solid #00d4ff;
        padding: 10px 14px;
        border-radius: 8px;
        margin-top: 10px;
        font-size: 0.95rem;
        direction: rtl !important;
        text-align: right !important;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. دالة مساعدة لاستدعاء Gemini بأمان داخل الواجهة
# ==========================================
def ask_gemini_safe(prompt_text: str, api_key: str) -> str:
    client = genai.Client(api_key=api_key)
    for model_name in ["gemini-3.0-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
        try:
            res = client.models.generate_content(model=model_name, contents=prompt_text)
            return res.text
        except Exception as e:
            if "404" in str(e) or "NOT_FOUND" in str(e):
                continue
            raise e
    for m in client.models.list():
        name = getattr(m, "name", "")
        if "gemini" in name:
            try:
                res = client.models.generate_content(model=name, contents=prompt_text)
                return res.text
            except Exception:
                continue
    raise RuntimeError("تعذر العثور على نموذج Gemini متاح في هذا الحساب.")

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
with st.sidebar:
    st.markdown("### 💠 مركز التحكم والربط")

    with st.expander("🔐 إعدادات المفاتيح والربط بالتلجرام (حفظ دائم)", expanded=False):
        gemini_key_input = st.text_input("مفتاح Gemini API", value=cfg.get("gemini_api_key", ""), type="password")
        tg_id_input = st.text_input("Telegram API ID", value=str(cfg.get("tg_api_id", "2040")))
        tg_hash_input = st.text_input("Telegram API Hash", value=cfg.get("tg_api_hash", "b18441a1ff607e10a989891a5462e627"))
        tg_group_input = st.text_input("رابط أو معرف القروب (اختياري)", value=cfg.get("tg_group", ""))
        bot_token_input = st.text_input("Bot Token", value=cfg.get("bot_token", ""), type="password")
        auto_reply_input = st.checkbox("تفعيل الرد الآلي في التلجرام", value=bool(cfg.get("auto_reply_enabled", False)))

        if st.button("💾 حفظ الإعدادات", use_container_width=True):
            cfg.update({
                "gemini_api_key": gemini_key_input.strip(),
                "tg_api_id": tg_id_input.strip(),
                "tg_api_hash": tg_hash_input.strip(),
                "tg_group": tg_group_input.strip(),
                "bot_token": bot_token_input.strip(),
                "auto_reply_enabled": auto_reply_input,
            })
            save_config(cfg)
            st.success("تم حفظ الإعدادات بنجاح!")

    with st.expander("🔄 سحب ومزامنة قروب التلجرام", expanded=True):
        limit_val = st.slider("عدد الرسائل المراد سحبها وتحليلها:", min_value=10, max_value=300, value=100, step=10)
        if st.button("بدء سحب وتصنيف رسائل القروب ⚡", use_container_width=True):
            with st.spinner("جاري سحب الرسائل وتحليلها بالذكاء الاصطناعي..."):
                cmd = [sys.executable, "telegram_scraper.py", "--limit", str(limit_val)]
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if proc.returncode == 0:
                    st.success("✅ اكتمل السحب والتحليل بنجاح!")
                    st.rerun()
                else:
                    st.error("تعذر إكمال السحب. راجع التفاصيل أدناه:")
                    st.code(proc.stderr or proc.stdout)

    with st.expander("🎙️📷 مختبر التحليل الذكي (نص / صورة / بصمة صوتية)", expanded=False):
        lab_text = st.text_area("أدخل نص النقاش أو وصف المشكلة:")
        lab_file = st.file_uploader("أرفق صورة أو ملفاً صوتياً (اختياري)", type=["png", "jpg", "jpeg", "mp3", "wav", "ogg", "m4a"])
        if st.button("تحليل وإضافة للقاعدة 🧠", use_container_width=True):
            if not cfg.get("gemini_api_key"):
                st.warning("يرجى إدخال مفتاح Gemini API أولاً.")
            elif not lab_text and not lab_file:
                st.warning("أدخل نصاً أو أرفق ملفاً للتحليل.")
            else:
                with st.spinner("جاري التحليل الفني..."):
                    try:
                        media_bytes = lab_file.read() if lab_file else None
                        mime_type = lab_file.type if lab_file else None
                        card = analyze_thread_with_ai(lab_text or "حلل المرفق", cfg["gemini_api_key"], media_bytes, mime_type)
                        msg_id = int(pd.Timestamp.now().timestamp())
                        save_card(card, msg_id, "", "مختبر التحليل", lab_text or "مرفق وسائط")
                        st.success(f"تم حفظ البطاقة: {card.get('title')}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"خطأ أثناء التحليل: {e}")

    with st.expander("➕ إضافة بطاقة معرفة يدوياً", expanded=False):
        m_title = st.text_input("عنوان البطاقة")
        m_model = st.selectbox("الطراز", ["Jaecoo J8", "Jaecoo J7", "جميع الطرازات / عام"])
        m_cat = st.selectbox("القسم", [
            "الأعطال والتشخيص",
            "الإصلاحات والحلول المؤكدة",
            "بنك القطع البديلة والمطابقات",
            "الأنظمة الذكية والشاشات",
            "الصيانة الدورية والوكالات",
            "الإكسسوارات والتجهيزات",
            "تجارب القيادة والأداء الفعلي",
            "نبض القروب والدردشة العامة",
        ])
        m_sub = st.selectbox("النظام الفرعي", [
            "المحرك والتيربو",
            "القير والدفع الرباعي AWD",
            "الكهرباء والبطارية والحساسات",
            "الشاشة والنظام الصوتي والربط",
            "التكييف والتبريد",
            "العضلات والفرامل والتعليق",
            "الهيكل الخارجي والمقصورة",
            "عام",
        ])
        m_prob = st.text_area("الوصف / المشكلة")
        m_sol = st.text_area("الخلاصة / الحل")
        m_parts = st.text_input("القطع والمواصفات الفنية")
        m_ver = st.checkbox("حل مؤكد ومجرب", value=True)

        if st.button("حفظ البطاقة اليدوية", use_container_width=True):
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
                save_card(card_manual, msg_id, "", "إضافة يدوية", m_prob)
                st.success("تمت إضافة البطاقة!")
                st.rerun()

    if st.button("📦 استعادة البيانات النموذجية", use_container_width=True):
        seed_sample_data()
        st.success("تمت استعادة البيانات النموذجية!")
        st.rerun()

# ==========================================
# 5. الواجهة الرئيسية وعرض البطاقات
# ==========================================
st.markdown("""
<div class="hero-box">
    <div class="hub-badge">JAECOO AI DIAGNOSTICS HUB</div>
    <h1 style="margin: 0; color: #ffffff;">💠 منصة جايكو الذكية</h1>
    <p style="color: #9ec5e8; margin-top: 8px;">منظومة ذكاء اصطناعي لتحليل وأرشفة الخبرات الفنية والأعطال والحلول لقروب ملاك جايكو</p>
</div>
""", unsafe_allow_html=True)

conn = get_db_connection()
df = pd.read_sql_query("SELECT * FROM knowledge_cards ORDER BY id DESC", conn)
conn.close()

col_m1, col_m2, col_m3 = st.columns(3)
with col_m1:
    st.markdown(f'<div class="metric-card"><div>💠 إجمالي البطاقات</div><div class="metric-val">{len(df)}</div></div>', unsafe_allow_html=True)
with col_m2:
    verified_count = int(df["is_verified_solution"].sum()) if not df.empty else 0
    st.markdown(f'<div class="metric-card"><div>✅ الحلول المؤكدة</div><div class="metric-val">{verified_count}</div></div>', unsafe_allow_html=True)
with col_m3:
    j8_count = int((df["car_model"] == "Jaecoo J8").sum()) if not df.empty else 0
    st.markdown(f'<div class="metric-card"><div>🚙 بطاقات Jaecoo J8</div><div class="metric-val">{j8_count}</div></div>', unsafe_allow_html=True)

st.write("")
col_s1, col_s2 = st.columns([2, 1])
with col_s1:
    search_q = st.text_input("🔍 البحث الفوري في قاعدة المعرفة", placeholder="ابحث عن: بطارية AGM، شاشة، تكييف، طقطقة...")
with col_s2:
    model_filter = st.selectbox("🚙 تصفية حسب الطراز:", ["الكل", "Jaecoo J8", "Jaecoo J7", "جميع الطرازات / عام"])

with st.expander("🤖 استشارة المهندس الذكي والموجز اليومي", expanded=False):
    user_q = st.text_input("اسأل المهندس الذكي عن أي عطل أو قطعة غيار:")
    c_btn1, c_btn2 = st.columns(2)
    with c_btn1:
        if st.button("بحث واستشارة فورية 💡", use_container_width=True):
            if not cfg.get("gemini_api_key"):
                st.warning("أدخل مفتاح Gemini API في الإعدادات أولاً.")
            elif user_q:
                with st.spinner("جاري فحص الأرشيف وتوليد الإجابة..."):
                    try:
                        db_ans = find_matching_solution_in_db(user_q, cfg["gemini_api_key"])
                        if db_ans:
                            st.info(db_ans)
                        else:
                            ai_ans = ask_gemini_safe(
                                f"أنت كبير مهندسي صيانة سيارات جايكو J7 و J8. أجب بدقة عن هذا الاستفسار:\n{user_q}",
                                cfg["gemini_api_key"],
                            )
                            st.info(ai_ans)
                    except Exception as e:
                        st.error(f"حدث خطأ: {e}")
    with c_btn2:
        if st.button("⚡ توليد الموجز الذكي لنبض القروب", use_container_width=True):
            if not cfg.get("gemini_api_key"):
                st.warning("أدخل مفتاح Gemini API في الإعدادات أولاً.")
            else:
                with st.spinner("جاري صياغة الموجز..."):
                    try:
                        pulse = generate_daily_pulse_summary(cfg["gemini_api_key"])
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

if filtered_df.empty:
    st.info("لا توجد بطاقات مطابقة حالياً. يمكنك الضغط على 'استعادة البيانات النموذجية' أو بدء سحب رسائل القروب من القائمة الجانبية.")
else:
    for _, row in filtered_df.iterrows():
        title_safe = html.escape(str(row.get("title", "")))
        prob_safe = html.escape(str(row.get("problem_or_topic", "")))
        sol_safe = html.escape(str(row.get("solution_or_summary", "")))
        parts_safe = html.escape(str(row.get("part_numbers_or_specs", "")))
        car_safe = html.escape(str(row.get("car_model", "")))
        sub_safe = html.escape(str(row.get("sub_system", "")))
        cat_safe = html.escape(str(row.get("category", "")))

        parts_html = f'<div class="k-section">🔧 <b>القطع والمواصفات الفنية:</b> {parts_safe}</div>' if parts_safe else ""

        st.markdown(f"""
        <div class="k-card">
            <div class="k-tags">
                <span class="k-tag">🚙 {car_safe}</span>
                <span class="k-tag">⚙️ {sub_safe}</span>
                <span class="k-tag">📂 {cat_safe}</span>
            </div>
            <div class="k-title">{title_safe}</div>
            <div class="k-section">🔹 <b>الوصف / المشكلة:</b> {prob_safe}</div>
            <div class="k-section">⚡ <b>الخلاصة / الحل:</b> {sol_safe}</div>
            {parts_html}
        </div>
        """, unsafe_allow_html=True)