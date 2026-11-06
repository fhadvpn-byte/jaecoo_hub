import asyncio
import argparse
from telethon import TelegramClient, events
from database_and_ai import (
    init_db,
    load_config,
    analyze_thread_with_ai,
    save_card,
    find_matching_solution_in_db,
)


def _config_ready(cfg: dict) -> bool:
    return bool(cfg.get("tg_api_id") and cfg.get("tg_api_hash") and cfg.get("tg_group") and cfg.get("gemini_api_key"))


async def sync_group_history(limit_messages: int = 150):
    init_db()
    cfg = load_config()

    if not _config_ready(cfg):
        print("❌ يرجى استكمال بيانات الربط (API ID, API Hash, Group, Gemini Key) من لوحة التحكم في التطبيق أولاً.")
        return

    client = TelegramClient("jaecoo_session", int(cfg["tg_api_id"]), cfg["tg_api_hash"])
    await client.start()
    print(f"💠 متصل بتلجرام.. جاري سحب آخر {limit_messages} رسالة من: {cfg['tg_group']}")
    entity = await client.get_entity(cfg["tg_group"])
    messages = await client.get_messages(entity, limit=limit_messages)
    messages = list(reversed(messages))

    threads = {}
    msg_to_root = {}
    for msg in messages:
        if not msg.text and not msg.media:
            continue
        sender = await msg.get_sender()
        sender_name = getattr(sender, "first_name", "عضو القروب") or "عضو القروب"
        msg_text = msg.text or ""
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            parent_id = msg.reply_to.reply_to_msg_id
            root_id = msg_to_root.get(parent_id, parent_id)
            msg_to_root[msg.id] = root_id
            if root_id in threads:
                threads[root_id]["replies"].append(f"- رد ({sender_name}): {msg_text}")
                # إذا كان الرد يحتوي على صورة قطعة غيار أو بصمة صوتية ولم يكن للأصل وسائط
                if msg.media and not threads[root_id]["media_msg"]:
                    threads[root_id]["media_msg"] = msg
        else:
            msg_to_root[msg.id] = msg.id
            threads[msg.id] = {
                "root_msg": msg,
                "media_msg": msg if msg.media else None,
                "author": sender_name,
                "main_text": f"طرح الموضوع ({sender_name}): {msg_text}",
                "replies": [],
            }

    print(f"🧠 تم تجميع {len(threads)} سلسلة نقاش. جاري الفرز بالذكاء الاصطناعي...")
    username = getattr(entity, "username", None)
    saved_count = 0
    for root_id, t_data in threads.items():
        full_text = t_data["main_text"]
        if t_data["replies"]:
            full_text += "\nسلسلة الردود:\n" + "\n".join(t_data["replies"])
        if len(full_text.strip()) < 25 and not t_data["media_msg"]:
            continue

        media_bytes = None
        mime_type = None
        # تحميل الصور أو البصمات الصوتية الصغيرة لتحليلها بـ Gemini Multimodal
        if t_data["media_msg"]:
            m_msg = t_data["media_msg"]
            file_size = getattr(m_msg.file, "size", 0) if m_msg.file else 0
            if m_msg.photo:
                media_bytes = await client.download_media(m_msg, bytes)
                mime_type = "image/jpeg"
            elif m_msg.voice or (m_msg.audio and file_size < 8_000_000):
                media_bytes = await client.download_media(m_msg, bytes)
                mime_type = "audio/ogg"

        thread_url = f"https://t.me/{username}/{root_id}" if username else f"https://t.me/c/{entity.id}/{root_id}"
        try:
            card = analyze_thread_with_ai(full_text, cfg["gemini_api_key"], media_bytes, mime_type)
            if card.get("is_worth_archiving"):
                save_card(card, root_id, thread_url, t_data["author"], full_text)
                saved_count += 1
                print(f"✅ [{card['category']}] {card['title']}")
        except Exception as e:
            print(f"⚠️ تخطي الرسالة {root_id}: {e}")

    print(f"🎉 اكتملت المزامنة! تم حفظ {saved_count} بطاقة معرفة جديدة.")
    await client.disconnect()


async def run_live_listener():
    """المراقب اللحظي: يلتقط النقاشات الحية ويرد آلياً على الاستفسارات المتكررة"""
    init_db()
    cfg = load_config()
    if not _config_ready(cfg):
        print("❌ يرجى استكمال بيانات الربط (API ID, API Hash, Group, Gemini Key) من لوحة التحكم في التطبيق أولاً.")
        return

    client = TelegramClient("jaecoo_session", int(cfg["tg_api_id"]), cfg["tg_api_hash"])
    await client.start()
    entity = await client.get_entity(cfg["tg_group"])
    print(f"📡 الرادار اللحظي يعمل الآن على قروب: {cfg['tg_group']}...")

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        msg = event.message
        if not msg.text and not msg.media:
            return
        sender = await msg.get_sender()
        sender_name = getattr(sender, "first_name", "عضو القروب") or "عضو القروب"

        # 1. إذا كانت رسالة سؤال جديد والرد الآلي مفعّل، نبحث في الحلول المعتمدة
        if not msg.is_reply and msg.text and len(msg.text) > 15 and cfg.get("auto_reply_enabled"):
            reply_text = find_matching_solution_in_db(msg.text, cfg["gemini_api_key"])
            if reply_text:
                await event.reply(reply_text)
                print(f"🤖 تم الرد آلياً على استفسار: {sender_name}")

        # 2. إذا كانت الرسالة رداً على موضوع، نجمع الأصل مع الرد لتحديث بطاقة المعرفة فوراً
        if msg.is_reply:
            orig_msg = await msg.get_reply_message()
            if orig_msg:
                orig_sender = await orig_msg.get_sender()
                orig_author = getattr(orig_sender, "first_name", "عضو") or "عضو"
                combined_text = (
                    f"الموضوع الأساسي ({orig_author}): {orig_msg.text or ''}\n"
                    f"الرد ({sender_name}): {msg.text or ''}"
                )
                username = getattr(entity, "username", None)
                url = f"https://t.me/{username}/{orig_msg.id}" if username else f"https://t.me/c/{entity.id}/{orig_msg.id}"
                try:
                    card = analyze_thread_with_ai(combined_text, cfg["gemini_api_key"])
                    if card.get("is_worth_archiving"):
                        save_card(card, orig_msg.id, url, orig_author, combined_text)
                        print(f"⚡ تحديث لحظي للبطاقة [{card['category']}]: {card['title']}")
                except Exception as e:
                    print(f"⚠️ خطأ في التحليل اللحظي: {e}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["history", "live"], default="history")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.mode == "history":
        asyncio.run(sync_group_history(args.limit))
    else:
        asyncio.run(run_live_listener())
