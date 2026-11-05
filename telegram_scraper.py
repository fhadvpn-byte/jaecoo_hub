import asyncio
import os
from telethon import TelegramClient
from database_and_ai import init_db, analyze_thread_with_ai, save_card

# إعدادات الاتصال بتلجرام (تحصل عليها من my.telegram.org)
API_ID = int(os.getenv("TG_API_ID", "12345678"))
API_HASH = os.getenv("TG_API_HASH", "your_telegram_api_hash")
GROUP_USERNAME = os.getenv("TG_GROUP", "jaecoo_ksa_group")  # معرف القروب أو رابطه
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "your_gemini_api_key")

async def fetch_and_process_group_history(limit_messages: int = 200):
    init_db()
    client = TelegramClient("jaecoo_session", API_ID, API_HASH)
    await client.start()
    print(f"✅ تم الاتصال بنجاح.. جاري سحب آخر {limit_messages} رسالة من القروب...")
    entity = await client.get_entity(GROUP_USERNAME)
    messages = await client.get_messages(entity, limit=limit_messages)
    # ترتيب الرسائل من الأقدم للأحدث لبناء شجرة الردود السياقية
    messages = list(reversed(messages))

    # قاموس لتجميع كل رسالة رئيسية مع جميع الردود التابعة لها
    threads = {}
    msg_to_root = {}
    for msg in messages:
        if not msg.text and not msg.media:
            continue

        sender = await msg.get_sender()
        sender_name = getattr(sender, "first_name", "عضو القروب") or "عضو القروب"
        msg_text = msg.text or "[مرفق وسائط: صورة / بصمة صوتية]"
        # التحقق مما إذا كانت الرسالة رداً على رسالة سابقة
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            parent_id = msg.reply_to.reply_to_msg_id
            root_id = msg_to_root.get(parent_id, parent_id)
            msg_to_root[msg.id] = root_id
            if root_id in threads:
                threads[root_id]["replies"].append(f"- رد من ({sender_name}): {msg_text}")
        else:
            # رسالة رئيسية جديدة تفتح موضوعاً
            msg_to_root[msg.id] = msg.id
            threads[msg.id] = {
                "root_msg": msg,
                "author": sender_name,
                "main_text": f"الموضوع الأساسي ({sender_name}): {msg_text}",
                "replies": []
            }

    print(f"🧠 تم تجميع {len(threads)} سلسلة نقاش سياقية. جاري التحليل بالذكاء الاصطناعي...")
    for root_id, thread_data in threads.items():
        full_thread_text = thread_data["main_text"]
        if thread_data["replies"]:
            full_thread_text += "\nالردود والتفاعل:\n" + "\n".join(thread_data["replies"])
        # تجاهل الرسائل القصيرة جداً التي ليس لها ردود
        if len(full_thread_text) < 25 and not thread_data["replies"]:
            continue
        # بناء رابط الرسالة المباشر في التلجرام
        username = getattr(entity, "username", None)
        thread_url = f"https://t.me/{username}/{root_id}" if username else f"https://t.me/c/{entity.id}/{root_id}"
        try:
            card = analyze_thread_with_ai(full_thread_text, GEMINI_API_KEY)
            if card.get("is_worth_archiving"):
                save_card(
                    card_data=card,
                    telegram_msg_id=root_id,
                    thread_url=thread_url,
                    author_name=thread_data["author"],
                    raw_text=full_thread_text
                )
                print(f"✔️ تم تصنيف وحفظ: [{card['category']}] -> {card['title']}")
        except Exception as e:
            print(f"⚠️ خطأ أثناء معالجة النقاش {root_id}: {e}")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(fetch_and_process_group_history(200))
