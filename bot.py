import logging

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, RPCError

import config
import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Client(
    "filestore_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)

# أنواع الملفات التي يمكن للأدمن رفعها كـ "برنامج"
MEDIA_FILTER = (
    filters.document
    | filters.video
    | filters.audio
    | filters.photo
)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def make_link(file_id: int) -> str:
    return f"https://t.me/{config.BOT_USERNAME}?start=file_{file_id}"


# ---------------------------------------------------------------------------
# /start (بدون بارامتر أو مع بارامتر ملف)
# ---------------------------------------------------------------------------

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    args = message.command

    if len(args) < 2:
        await message.reply_text(
            "أهلاً بك! 👋\n"
            "أرسل لك البرنامج المطلوب فور توفر رابط مباشر له من القناة."
        )
        return

    param = args[1]
    if not param.startswith("file_"):
        await message.reply_text("رابط غير صالح.")
        return

    try:
        file_id = int(param.replace("file_", "", 1))
    except ValueError:
        await message.reply_text("رابط غير صالح.")
        return

    await deliver_file(client, message.chat.id, file_id, message)


async def deliver_file(client: Client, chat_id: int, file_id: int, trigger_message: Message):
    file_row = db.get_file(file_id)
    if file_row is None:
        await client.send_message(chat_id, "لم يتم العثور على هذا الملف، ربما تم حذفه.")
        return

    storage_message_id, caption = file_row

    # تحقق من الاشتراك الإجباري إن كان مفعّلًا
    if db.is_force_sub_enabled():
        channel = db.get_force_sub_channel()
        if channel:
            subscribed = await check_subscription(client, chat_id, channel)
            if not subscribed:
                await ask_to_subscribe(client, chat_id, channel, file_id)
                return

    try:
        await client.copy_message(
            chat_id=chat_id,
            from_chat_id=config.STORAGE_CHANNEL_ID,
            message_id=storage_message_id,
        )
    except RPCError as e:
        logger.exception(e)
        await client.send_message(chat_id, "حدث خطأ أثناء إرسال الملف، حاول لاحقًا.")


async def check_subscription(client: Client, user_id: int, channel: str) -> bool:
    try:
        member = await client.get_chat_member(channel, user_id)
        return member.status not in ("left", "kicked", "banned")
    except UserNotParticipant:
        return False
    except ChatAdminRequired:
        # البوت ليس أدمن في قناة الاشتراك، لا يمكن التحقق - نسمح بالمرور لتجنب حجب الجميع
        logger.warning("البوت ليس أدمن في قناة الاشتراك الإجباري، لا يمكن التحقق.")
        return True
    except RPCError as e:
        logger.exception(e)
        return True


async def ask_to_subscribe(client: Client, chat_id: int, channel: str, file_id: int):
    # بناء رابط القناة للزر
    channel_link = channel
    if not str(channel).startswith("http") and not str(channel).startswith("@"):
        # آيدي رقمي - لا يمكن بناء رابط مباشر منه تلقائيًا بدون يوزرنيم
        channel_link = None

    buttons = []
    if channel_link:
        display_link = channel_link if channel_link.startswith("http") else f"https://t.me/{channel_link.lstrip('@')}"
        buttons.append([InlineKeyboardButton("📢 اشترك بالقناة", url=display_link)])

    buttons.append(
        [InlineKeyboardButton("✅ تحققت، أعطني الملف", callback_data=f"check_sub:{file_id}")]
    )

    await client.send_message(
        chat_id,
        "⚠️ يجب عليك الاشتراك بالقناة أولًا لاستلام الملف.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@app.on_callback_query(filters.regex(r"^check_sub:(\d+)$"))
async def check_sub_callback(client: Client, callback: CallbackQuery):
    file_id = int(callback.matches[0].group(1))
    channel = db.get_force_sub_channel()

    if not channel or not db.is_force_sub_enabled():
        await deliver_file(client, callback.message.chat.id, file_id, callback.message)
        await callback.message.delete()
        return

    subscribed = await check_subscription(client, callback.from_user.id, channel)
    if subscribed:
        await callback.answer("تم التحقق ✅")
        await deliver_file(client, callback.message.chat.id, file_id, callback.message)
        await callback.message.delete()
    else:
        await callback.answer("لم يتم رصد اشتراكك بعد، اشترك ثم حاول مجددًا.", show_alert=True)


# ---------------------------------------------------------------------------
# رفع ملف جديد من الأدمن
# ---------------------------------------------------------------------------

@app.on_message(filters.private & MEDIA_FILTER & filters.user(config.ADMIN_IDS))
async def admin_upload_handler(client: Client, message: Message):
    caption = message.caption or ""

    # نسخ الملف إلى قناة التخزين الخاصة
    stored = await message.copy(chat_id=config.STORAGE_CHANNEL_ID)

    file_id = db.add_file(
        storage_message_id=stored.id,
        caption=caption,
        added_by=message.from_user.id,
    )

    link = make_link(file_id)
    await message.reply_text(
        f"✅ تم الحفظ بنجاح.\n\n"
        f"🆔 معرف الملف: `{file_id}`\n"
        f"🔗 الرابط:\n{link}",
        quote=True,
    )


# ---------------------------------------------------------------------------
# أوامر إدارة الاشتراك الإجباري (للأدمن فقط)
# ---------------------------------------------------------------------------

@app.on_message(filters.command("forcesub") & filters.private & filters.user(config.ADMIN_IDS))
async def forcesub_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        current = "مفعّل ✅" if db.is_force_sub_enabled() else "متوقف ❌"
        await message.reply_text(
            f"الحالة الحالية للاشتراك الإجباري: {current}\n\n"
            "الاستخدام:\n"
            "`/forcesub on` لتفعيله\n"
            "`/forcesub off` لإيقافه"
        )
        return

    enabled = args[1].lower() == "on"
    db.set_force_sub_enabled(enabled)

    if enabled and not db.get_force_sub_channel():
        await message.reply_text(
            "✅ تم تفعيل الاشتراك الإجباري.\n"
            "⚠️ لكن لم تُحدّد قناة بعد، استخدم:\n"
            "`/setchannel @channel_username` أو `/setchannel -100xxxxxxxxxx`"
        )
    else:
        status = "تفعيل" if enabled else "إيقاف"
        await message.reply_text(f"✅ تم {status} الاشتراك الإجباري.")


@app.on_message(filters.command("setchannel") & filters.private & filters.user(config.ADMIN_IDS))
async def setchannel_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        await message.reply_text(
            "أرسل يوزر القناة أو آيديها:\n"
            "`/setchannel @channel_username`\n"
            "أو\n"
            "`/setchannel -1001234567890`"
        )
        return

    channel = args[1]
    db.set_force_sub_channel(channel)
    await message.reply_text(f"✅ تم تعيين قناة الاشتراك الإجباري إلى: {channel}")


@app.on_message(filters.command("status") & filters.private & filters.user(config.ADMIN_IDS))
async def status_handler(client: Client, message: Message):
    force_sub = "مفعّل ✅" if db.is_force_sub_enabled() else "متوقف ❌"
    channel = db.get_force_sub_channel() or "غير محدد"
    await message.reply_text(
        f"📊 حالة البوت:\n"
        f"الاشتراك الإجباري: {force_sub}\n"
        f"قناة الاشتراك: {channel}"
    )


if __name__ == "__main__":
    db.init_db()
    logger.info("Bot is starting...")
    app.run()
