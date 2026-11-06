import asyncio
import argparse
import logging
import os
import shutil
import sqlite3
from telethon import TelegramClient
from database_and_ai import init_db, load_config, analyze_thread_with_ai, save_card

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

def prepare_unlocked_session() -> str:
    """ينشئ نسخة عمل مؤقتة من الجلسة ويفك أي قفل في SQLite لمنع خطأ database is locked نهائياً"""
    src = "user_session.session"
    dst = "scraper_runtime.session"
    if os.path.exists(src):
        try:
            shutil.copy2(src, dst)
            conn = sqlite3.connect(dst, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.commit()
            conn.close()
            return "scraper_runtime"
        except Exception:
            pass
    return "user_session"

async def sync_and_analyze(limit=50):
    logger.info("جاري تهيئة قاعدة البيانات والإعدادات...")
    init_db()
    cfg = load_config()

    gemini_key = cfg.get("gemini_api_key", "").strip()
    if not gemini_key:
        logger.error("⚠️ مفتاح Gemini API غير موجود في config.json!")
        return

    api_id = int(cfg.get("tg_api_id") or DEFAULT_API_ID)
    api_hash = cfg.get("tg_api_hash") or DEFAULT_API_HASH

    session_name = prepare_unlocked_session()
    client = TelegramClient(session_name, api_id, api_hash, timeout=30)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("⚠️ الجلسة غير موثقة! يرجى توثيق الحساب من الطرفية أولاً.")
            return

        logger.info("✅ تم الاتصال بالتلجرام بنجاح! جاري فحص القروبات...")

        async for dialog in client.iter_dialogs(limit=15):
            if not (dialog.is_group or dialog.is_channel):
                continue

            logger.info(f"📂 فحص القروب: {dialog.name} (ID: {dialog.id})")
            messages = await client.get_messages(dialog.id, limit=limit)

            for message in messages:
                if message.text and len(message.text.strip()) > 15:
                    sender = await message.get_sender()
                    author_name = getattr(sender, "first_name", None) or "عضو"
                    combined_text = f"محتوى الرسالة: {message.text}"

                    try:
                        card = analyze_thread_with_ai(combined_text, gemini_key)
                        if card and card.get("is_worth_archiving"):
                            chat_id_clean = str(dialog.id).replace("-100", "").replace("-", "")
                            thread_url = f"https://t.me/c/{chat_id_clean}/{message.id}"
                            save_card(card, message.id, thread_url, author_name, combined_text)
                            logger.info(f"✨ تم حفظ بطاقة: [{card.get('category')}] {card.get('title')}")
                    except Exception as ai_err:
                        logger.error(f"خطأ في التحليل: {ai_err}")

        logger.info("🎉 تم الانتهاء من السحب والتحليل بنجاح تام!")

    except Exception as e:
        logger.error(f"حدث خطأ أثناء السحب: {e}")
    finally:
        try:
            await client.disconnect()
        except sqlite3.OperationalError:
            # تجاهل قفل التحديث الروتيني عند قطع الاتصال لأن الرسائل حُفظت بالفعل
            pass
        except Exception:
            pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mode", type=str, default="history")
    args, _ = parser.parse_known_args()

    asyncio.run(sync_and_analyze(limit=args.limit))