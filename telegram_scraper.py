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

# معرّفات عميل تلجرام العامة التي يحتاجها Telethon حتى عند الدخول بتوكن البوت.
# التوكن نفسه يبقى في config.json ولا يُكتب هنا.
DEFAULT_BOT_API_ID = 2040
DEFAULT_BOT_API_HASH = "b18441a1ff607e10a989891a5462e627"
MAX_AUDIO_BYTES = 8_000_000


def _client_credentials(cfg: dict) -> tuple[int, str]:
    api_id = str(cfg.get("tg_api_id") or DEFAULT_BOT_API_ID)
    api_hash = cfg.get("tg_api_hash") or DEFAULT_BOT_API_HASH
    return int(api_id), api_hash


def _public_chat_id(chat_id) -> str:
    raw = str(chat_id)
    if raw.startswith("-100"):
        return raw[4:]
    return raw.lstrip("-")


async def sync_bot_group_history(limit_messages: int = 100):
    init_db()
    cfg = load_config()

    if not cfg.get("gemini_api_key"):
        print("❌ يرجى إدخال مفتاح Gemini API في التطبيق أولاً.")
        return
    if not cfg.get("bot_token"):
        print("❌ يرجى إدخال توكن البوت من لوحة التحكم ثم حفظ الإعدادات.")
        return

    api_id, api_hash = _client_credentials(cfg)
    client = TelegramClient("jaecoo_bot_session", api_id, api_hash)
    await client.start(bot_token=cfg["bot_token"])

    print("🤖 تم الاتصال ببوت تلجرام بنجاح..")

    dialogs = await client.get_dialogs()
    target_dialog = None
    wanted = (cfg.get("tg_group") or "").strip().lstrip("@").lower()
    for dialog in dialogs:
        if not (dialog.is_group or dialog.is_channel):
            continue
        name = (dialog.name or "").lower()
        username = (getattr(dialog.entity, "username", None) or "").lower()
        if wanted and wanted not in {name, username} and wanted not in name:
            continue
        target_dialog = dialog
        break

    if not target_dialog:
        print("⚠️ لم يتم العثور على قروب مشترك فيه البوت! تأكد من إضافة البوت إلى القروب ومنحه صلاحية قراءة الرسائل.")
        await client.disconnect()
        return

    print(f"📥 جاري سحب آخر {limit_messages} رسالة من القروب: {target_dialog.name}...")
    messages = await client.get_messages(target_dialog.entity, limit=limit_messages)
    messages = list(reversed(messages))

    threads = {}
    msg_to_root = {}
    for msg in messages:
        if not msg.text and not msg.media:
            continue
        try:
            sender = await msg.get_sender()
            sender_name = getattr(sender, "first_name", "عضو القروب") or "عضو القروب"
        except Exception:
            sender_name = "عضو القروب"

        msg_text = msg.text or ""
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            parent_id = msg.reply_to.reply_to_msg_id
            root_id = msg_to_root.get(parent_id, parent_id)
            msg_to_root[msg.id] = root_id
            if root_id in threads:
                threads[root_id]["replies"].append(f"- رد ({sender_name}): {msg_text}")
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
    saved_count = 0
    chat_id_str = _public_chat_id(target_dialog.id)
    for root_id, t_data in threads.items():
        full_text = t_data["main_text"]
        if t_data["replies"]:
            full_text += "\nسلسلة الردود:\n" + "\n".join(t_data["replies"])
        if len(full_text.strip()) < 15 and not t_data["media_msg"]:
            continue

        media_bytes = None
        mime_type = None
        if t_data["media_msg"]:
            m_msg = t_data["media_msg"]
            file_size = getattr(m_msg.file, "size", 0) if m_msg.file else 0
            try:
                if m_msg.photo:
                    media_bytes = await client.download_media(m_msg, bytes)
                    mime_type = "image/jpeg"
                elif m_msg.voice or (m_msg.audio and file_size < MAX_AUDIO_BYTES):
                    media_bytes = await client.download_media(m_msg, bytes)
                    mime_type = "audio/ogg"
            except Exception:
                pass

        thread_url = f"https://t.me/c/{chat_id_str}/{root_id}"
        try:
            card = analyze_thread_with_ai(full_text, cfg["gemini_api_key"], media_bytes, mime_type)
            if card.get("is_worth_archiving"):
                save_card(card, root_id, thread_url, t_data["author"], full_text)
                saved_count += 1
                print(f"✅ [{card['category']}] {card['title']}")
        except Exception as e:
            print(f"⚠️ تخطي الرسالة {root_id}: {e}")

    print(f"🎉 اكتملت المزامنة بنجاح! تمت إضافة {saved_count} بطاقة معرفة إلى التطبيق.")
    await client.disconnect()


async def run_bot_live_listener():
    """المراقب اللحظي للبوت: يقرأ رسائل القروب لحظة بلحظة"""
    init_db()
    cfg = load_config()
    if not cfg.get("gemini_api_key"):
        print("❌ يرجى إدخال مفتاح Gemini API في التطبيق أولاً.")
        return
    if not cfg.get("bot_token"):
        print("❌ يرجى إدخال توكن البوت من لوحة التحكم ثم حفظ الإعدادات.")
        return

    api_id, api_hash = _client_credentials(cfg)
    client = TelegramClient("jaecoo_bot_session", api_id, api_hash)
    await client.start(bot_token=cfg["bot_token"])
    print("📡 رادار البوت اللحظي يعمل الآن ويستمع لرسائل القروب...")

    @client.on(events.NewMessage)
    async def handler(event):
        if event.is_private:
            return
        msg = event.message
        if not msg.text and not msg.media:
            return
        try:
            sender = await msg.get_sender()
            sender_name = getattr(sender, "first_name", "عضو") or "عضو"
        except Exception:
            sender_name = "عضو"

        msg_text = msg.text or ""
        if not msg.is_reply and len(msg_text) > 15 and cfg.get("auto_reply_enabled"):
            reply_text = find_matching_solution_in_db(msg_text, cfg["gemini_api_key"])
            if reply_text:
                await event.reply(reply_text)
                print(f"🤖 رد البوت آلياً على: {sender_name}")

        if msg.is_reply:
            orig_msg = await msg.get_reply_message()
            if orig_msg:
                try:
                    orig_sender = await orig_msg.get_sender()
                    orig_author = getattr(orig_sender, "first_name", "عضو") or "عضو"
                except Exception:
                    orig_author = "عضو"
                combined_text = (
                    f"الموضوع الأساسي ({orig_author}): {orig_msg.text or ''}\n"
                    f"الرد ({sender_name}): {msg_text}"
                )
                url = f"https://t.me/c/{_public_chat_id(event.chat_id)}/{orig_msg.id}"
                try:
                    card = analyze_thread_with_ai(combined_text, cfg["gemini_api_key"])
                    if card.get("is_worth_archiving"):
                        save_card(card, orig_msg.id, url, orig_author, combined_text)
                        print(f"⚡ تحديث لحظي للبطاقة [{card['category']}]: {card['title']}")
                except Exception as e:
                    print(f"⚠️ خطأ بالتحليل اللحظي: {e}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["history", "live"], default="history")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.mode == "history":
        asyncio.run(sync_bot_group_history(args.limit))
    else:
        asyncio.run(run_bot_live_listener())
