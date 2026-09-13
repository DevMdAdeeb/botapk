import logging
import asyncio
import re
from typing import Dict, Any, List, Optional

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, RPCError, FloodWait

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

# أنواع الملفات التي يمكن للأدمن رفعها لتخزينها
MEDIA_FILTER = (
    filters.document
    | filters.video
    | filters.audio
    | filters.photo
)

# القاموس المخصص لإدارة حالات الأدمن التفاعلية (Waiting state)
# ADMIN_STATES[user_id] = {"action": "waiting_...", ...}
ADMIN_STATES: Dict[int, Dict[str, Any]] = {}


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def make_link(file_id: int) -> str:
    return f"https://t.me/{config.BOT_USERNAME}?start=file_{file_id}"


def sanitize_channel_input(input_str: str) -> str:
    """تنظيف مدخلات اسم القناة وتحويل الروابط إلى يوزرنيم أو آيدي رقمي صريح."""
    cleaned = input_str.strip()
    if cleaned.startswith("https://t.me/") or cleaned.startswith("http://t.me/"):
        cleaned = cleaned.rstrip("/").split("/")[-1]
        if not cleaned.startswith("@") and not cleaned.startswith("-100") and not cleaned.isdigit():
            cleaned = f"@{cleaned}"
    elif not cleaned.startswith("@") and not cleaned.startswith("-100") and not cleaned.isdigit():
        cleaned = f"@{cleaned}"
    return cleaned


def build_post_keyboard(download_link: str, extra_buttons: List[Dict[str, str]] = None) -> InlineKeyboardMarkup:
    """يبني لوحة الأزرار الشفافة للمنشور شاملاً زر التحميل والأزرار الإضافية."""
    rows = [
        [InlineKeyboardButton("اضغط هنا للتحميل  📥", url=download_link)]
    ]
    if extra_buttons:
        for btn in extra_buttons:
            text = btn.get("text")
            url = btn.get("url")
            if text and url:
                rows.append([InlineKeyboardButton(text, url=url)])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# لوحة تحكم الأدمن
# ---------------------------------------------------------------------------

def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 إنشاء منشور تطبيق", callback_data="admin_create_post_prompt"),
            InlineKeyboardButton("📚 المنشورات المحفوظة", callback_data="admin_posts:0"),
        ],
        [
            InlineKeyboardButton("🔥 الأعلى تحميلاً (>10)", callback_data="admin_top_downloads"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("📢 الإذاعة العامة", callback_data="admin_broadcast_prompt"),
            InlineKeyboardButton("🔐 الاشتراك الإجباري", callback_data="admin_forcesub"),
        ],
        [
            InlineKeyboardButton("📢 قنوات النشر العامة", callback_data="admin_public_channels"),
            InlineKeyboardButton("📁 إدارة الملفات", callback_data="admin_files:0"),
        ],
        [
            InlineKeyboardButton("❌ إغلاق اللوحة", callback_data="admin_close")
        ]
    ])


# ---------------------------------------------------------------------------
# التفاعل مع الرسائل الواردة (/start والأوامر والنصوص)
# ---------------------------------------------------------------------------

@app.on_message(filters.private & filters.command("start"))
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or ""
    username = message.from_user.username or ""

    # تسجيل المستخدم في قاعدة البيانات
    db.add_user(user_id, first_name, username)

    args = message.command

    # 1. دخول عبر رابط ملف
    if len(args) >= 2 and args[1].startswith("file_"):
        param = args[1]
        try:
            file_id = int(param.replace("file_", "", 1))
        except ValueError:
            await message.reply_text("❌ الرابط غير صالح.")
            return

        await deliver_file(client, user_id, file_id, message)
        return

    # 2. دخول أدمن عادي بدون رابط ملف
    if is_admin(user_id):
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎛 فتح لوحة التحكم", callback_data="admin_panel")]
        ])
        await message.reply_text(
            "أهلاً بك يا مدير 👋\n"
            "اضغط على الزر أدناه للوصول إلى لوحة التحكم الشفافة.",
            reply_markup=buttons
        )
        return

    # 3. دخول مستخدم عادي (بدون رابط ملف)
    # تحقق من الاشتراك الإجباري أولاً
    if db.is_force_sub_enabled():
        channels = db.get_force_sub_channels()
        if channels:
            unsubscribed_channels = []
            for ch in channels:
                subscribed = await check_subscription(client, user_id, ch)
                if not subscribed:
                    unsubscribed_channels.append(ch)

            if unsubscribed_channels:
                await ask_to_subscribe(client, user_id, unsubscribed_channels, file_id=0)
                return

    # إذا كان مشتركاً أو الاشتراك غير مفعل
    await message.reply_text(
        "أهلاً بك! 👋\n"
        "أرسل لك البرنامج المطلوب فور توفر رابط مباشر له من القناة."
    )


# ---------------------------------------------------------------------------
# التسليم والتحقق من الاشتراك الإجباري
# ---------------------------------------------------------------------------

async def deliver_file(client: Client, chat_id: int, file_id: int, trigger_message: Message):
    file_row = db.get_file(file_id)
    if file_row is None:
        await client.send_message(chat_id, "❌ لم يتم العثور على هذا الملف، ربما تم حذفه.")
        return

    storage_message_id, caption, downloads, created_at = file_row

    # تحقق من الاشتراك الإجباري إن كان مفعّلاً
    if db.is_force_sub_enabled():
        channels = db.get_force_sub_channels()
        if channels:
            unsubscribed_channels = []
            for ch in channels:
                subscribed = await check_subscription(client, chat_id, ch)
                if not subscribed:
                    unsubscribed_channels.append(ch)

            if unsubscribed_channels:
                await ask_to_subscribe(client, chat_id, unsubscribed_channels, file_id)
                return

    try:
        await client.copy_message(
            chat_id=chat_id,
            from_chat_id=config.STORAGE_CHANNEL_ID,
            message_id=storage_message_id,
        )
        db.increment_downloads(file_id)
    except RPCError as e:
        logger.exception(e)
        await client.send_message(chat_id, "⚠️ حدث خطأ أثناء إرسال الملف، حاول لاحقًا.")


async def check_subscription(client: Client, user_id: int, channel: str) -> bool:
    try:
        ch_target = int(channel) if (str(channel).startswith("-100") or str(channel).isdigit()) else channel
        member = await client.get_chat_member(ch_target, user_id)
        return member.status not in ("left", "kicked", "banned")
    except UserNotParticipant:
        return False
    except ChatAdminRequired:
        logger.warning(f"البوت ليس أدمن في القناة {channel}، يتعذر التحقق.")
        return True
    except RPCError as e:
        logger.exception(e)
        return True


async def get_channel_link(client: Client, channel: str) -> Optional[str]:
    """يجلب رابط القناة تلقائياً سواء كانت يوزر أو آيدي رقمي."""
    try:
        ch_target = int(channel) if (str(channel).startswith("-100") or str(channel).isdigit()) else channel
        chat = await client.get_chat(ch_target)
        if chat.username:
            return f"https://t.me/{chat.username}"
        elif chat.invite_link:
            return chat.invite_link
        else:
            try:
                invite = await client.export_chat_invite_link(ch_target)
                return invite
            except Exception:
                return None
    except Exception as e:
        logger.error(f"Error getting chat link for {channel}: {e}")
        if str(channel).startswith("@"):
            return f"https://t.me/{channel.lstrip('@')}"
        elif str(channel).startswith("http://") or str(channel).startswith("https://"):
            return channel
        return None


async def ask_to_subscribe(client: Client, chat_id: int, channels: list, file_id: int):
    buttons = []
    for idx, ch in enumerate(channels, 1):
        link = await get_channel_link(client, ch)
        if link:
            buttons.append([InlineKeyboardButton(f"📢 اشترك في القناة ({idx})", url=link)])

    buttons.append(
        [InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data=f"check_sub:{file_id}")]
    )

    await client.send_message(
        chat_id,
        "⚠️ **عذراً، يجب عليك الاشتراك في القنوات التالية أولاً لاستخدام البوت:**",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@app.on_callback_query(filters.regex(r"^check_sub:(\d+)$"))
async def check_sub_callback(client: Client, callback: CallbackQuery):
    file_id = int(callback.matches[0].group(1))
    user_id = callback.from_user.id

    if not db.is_force_sub_enabled():
        await callback.message.delete()
        if file_id > 0:
            await deliver_file(client, user_id, file_id, callback.message)
        else:
            await callback.message.reply_text("أهلاً بك! 👋\nأرسل لك البرنامج المطلوب فور توفر رابط مباشر له من القناة.")
        return

    channels = db.get_force_sub_channels()
    unsubscribed = []
    for ch in channels:
        if not await check_subscription(client, user_id, ch):
            unsubscribed.append(ch)

    if not unsubscribed:
        await callback.answer("تم التحقق بنجاح ✅")
        await callback.message.delete()
        if file_id > 0:
            await deliver_file(client, user_id, file_id, callback.message)
        else:
            await callback.message.reply_text("أهلاً بك! 👋\nأرسل لك البرنامج المطلوب فور توفر رابط مباشر له من القناة.")
    else:
        await callback.answer("❌ لم يتم رصد اشتراكك في جميع القنوات بعد!", show_alert=True)


# ---------------------------------------------------------------------------
# رفع الملفات وإدارة الحالات التفاعلية للأدمن
# ---------------------------------------------------------------------------

@app.on_message(filters.private & MEDIA_FILTER & filters.user(config.ADMIN_IDS))
async def admin_media_handler(client: Client, message: Message):
    user_id = message.from_user.id

    if user_id in ADMIN_STATES:
        await process_admin_state_input(client, message)
        return

    # الرفع المباشر السريع العادي للملفات بدون تدفق المنشور
    caption = message.caption or ""

    stored = await message.copy(chat_id=config.STORAGE_CHANNEL_ID)

    file_id = db.add_file(
        storage_message_id=stored.id,
        caption=caption,
        added_by=user_id,
    )

    link = make_link(file_id)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 تفاصيل الملف", callback_data=f"file_info:{file_id}")],
        [InlineKeyboardButton("🎛 لوحة التحكم", callback_data="admin_panel")]
    ])

    await message.reply_text(
        f"✅ **تم حفظ الملف بنجاح!**\n\n"
        f"🆔 معرف الملف: `{file_id}`\n"
        f"🔗 الرابط المباشر:\n{link}",
        reply_markup=buttons,
        quote=True,
    )


@app.on_message(filters.private & ~filters.command("start") & filters.user(config.ADMIN_IDS))
async def admin_text_router(client: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""

    # المعالجة الخاصة بالأوامر النسيجية: "معاينة X" أو "ارسال X"
    if text.startswith("معاينة ") or text.startswith("معاينه "):
        post_id_str = text.split(" ", 1)[1].strip()
        if post_id_str.isdigit():
            await preview_post_by_id(client, user_id, int(post_id_str))
            return

    if text.startswith("ارسال ") or text.startswith("إرسال "):
        post_id_str = text.split(" ", 1)[1].strip()
        if post_id_str.isdigit():
            await publish_post_by_id(client, user_id, int(post_id_str))
            return

    if user_id in ADMIN_STATES:
        await process_admin_state_input(client, message)
    else:
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 إنشاء منشور تطبيق", callback_data="admin_create_post_prompt")],
            [InlineKeyboardButton("🎛 فتح لوحة التحكم", callback_data="admin_panel")]
        ])
        await message.reply_text("🎛 يمكنك التحكم بالبوت أو تحرير المنشورات عبر الأزرار أدناه:", reply_markup=buttons)


# ---------------------------------------------------------------------------
# معالجة التدفقات التفاعلية للأدمن (State Processor)
# ---------------------------------------------------------------------------

async def process_admin_state_input(client: Client, message: Message):
    user_id = message.from_user.id
    state_info = ADMIN_STATES.get(user_id)
    if not state_info:
        return

    action = state_info.get("action")

    # 1. تدفق إنشاء منشور التطبيق - الخطوة الأولى: استقبال المنشور
    if action == "waiting_post_content":
        ADMIN_STATES[user_id] = {
            "action": "waiting_app_file",
            "post_message_id": message.id,
            "post_chat_id": message.chat.id,
            "extra_buttons": []
        }

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="post_cancel")]
        ])

        await message.reply_text(
            "✅ **تم استلام المنشور بنجاح!**\n\n"
            "📥 الآن، يرجى إرسال **ملف/تطبيق البرنامج** ليتم ربطه بهذا المنشور وتوليد زر التحميل.",
            reply_markup=buttons,
            reply_to_message_id=message.id
        )

    # 1. تدفق إنشاء منشور التطبيق - الخطوة الثانية: استقبال ملف التطبيق
    elif action == "waiting_app_file":
        if not (message.document or message.video or message.audio or message.photo):
            await message.reply_text("❌ يرجى إرسال ملف التطبيق (ملف، فيديو، صوت، أو صورة) لربطه بالمنشور.")
            return

        stored = await message.copy(chat_id=config.STORAGE_CHANNEL_ID)
        caption = message.caption or ""
        file_id = db.add_file(
            storage_message_id=stored.id,
            caption=caption,
            added_by=user_id,
        )

        state_info["file_id"] = file_id
        state_info["action"] = "waiting_extra_buttons_prompt"

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة زر خارجي", callback_data="post_add_button_prompt")],
            [InlineKeyboardButton("➡️ المتابعة بدون أزرار إضافية", callback_data="post_finish_buttons")]
        ])

        await message.reply_text(
            "✅ **تم ربط التطبيق بنجاح!**\n\n"
            "هل ترغب في إضافة أزرار خارجية إضافية للمنشور؟ (مثلاً: زر مشاهدة الشرح، رابط الموقع الرسمي...)",
            reply_markup=buttons
        )

    # 1. تدفق إنشاء منشور التطبيق - الخطوة الثالثة: استقبال الزر الخارجي
    elif action == "waiting_button_input":
        text_input = message.text.strip() if message.text else ""
        if "-" not in text_input or not (text_input.startswith("http://") or "http" in text_input):
            await message.reply_text(
                "❌ صيغة غير صحيحة! يرجى الإرسال بالشكل التالي:\n"
                "`اسم الزر - https://example.com`"
            )
            return

        parts = text_input.split("-", 1)
        btn_text = parts[0].strip()
        btn_url = parts[1].strip()

        extra_buttons = state_info.get("extra_buttons", [])
        extra_buttons.append({"text": btn_text, "url": btn_url})
        state_info["extra_buttons"] = extra_buttons

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة زر آخر", callback_data="post_add_button_prompt")],
            [InlineKeyboardButton("✅ الإنتهاء والمعاينة", callback_data="post_finish_buttons")]
        ])

        await message.reply_text(
            f"✅ تم إضافة الزر: **[{btn_text}]({btn_url})**\n"
            f"إجمالي الأزرار الإضافية: `{len(extra_buttons)}`",
            reply_markup=buttons
        )

    # 2. حالة إضافة قناة اشتراك إجباري
    elif action == "waiting_add_channel":
        raw_text = message.text.strip() if message.text else ""
        if not raw_text:
            await message.reply_text("❌ يرجى إرسال يوزر القناة (مثال: `@mychannel`) أو رابطها أو آيدي القناة.")
            return

        channel_text = sanitize_channel_input(raw_text)

        # فحص إمكانية الوصول للقناة والتأكد إن كان البوت أدمن فيها
        try:
            ch_target = int(channel_text) if (channel_text.startswith("-100") or channel_text.isdigit()) else channel_text
            chat = await client.get_chat(ch_target)
            member = await client.get_chat_member(chat.id, "me")
            if member.status not in ("administrator", "creator"):
                warning_msg = "\n\n⚠️ **تنبيه هام:** البوت ليس أدمن في هذه القناة! يرجى رفع البوت أدمن في القناة ليتمكن من التحقق من اشتراكات المستخدمين."
            else:
                warning_msg = ""
        except Exception:
            warning_msg = "\n\n⚠️ **تنبيه:** تعذر التحقق التلقائي من القناة حالياً، تأكد من رفع البوت أدمن فيها وصحة المعرف."

        success = db.add_force_sub_channel(channel_text)
        ADMIN_STATES.pop(user_id, None)

        if success:
            await message.reply_text(
                f"✅ تم إضافة القناة `{channel_text}` بنجاح!{warning_msg}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="admin_forcesub")]])
            )
        else:
            await message.reply_text(
                f"⚠️ القناة `{channel_text}` مضافة بالفعل سابقاً.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="admin_forcesub")]])
            )

    # 3. حالة إضافة قناة نشر عامة
    elif action == "waiting_add_public_channel":
        raw_text = message.text.strip() if message.text else ""
        if not raw_text:
            await message.reply_text("❌ يرجى إرسال يوزر القناة (مثال: `@mychannel`) أو رابطها أو آيدي القناة.")
            return

        channel_text = sanitize_channel_input(raw_text)
        success = db.add_public_channel(channel_text)
        ADMIN_STATES.pop(user_id, None)

        if success:
            await message.reply_text(
                f"✅ تم إضافة قناة النشر `{channel_text}` بنجاح!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة لقنوات النشر", callback_data="admin_public_channels")]])
            )
        else:
            await message.reply_text(
                f"⚠️ القناة `{channel_text}` مضافة بالفعل سابقاً في قنوات النشر.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة لقنوات النشر", callback_data="admin_public_channels")]])
            )

    # 4. حالة تجهيز الإذاعة
    elif action == "waiting_broadcast_msg":
        ADMIN_STATES[user_id] = {
            "action": "confirm_broadcast",
            "message_id": message.id,
            "chat_id": message.chat.id
        }

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 تأكيد الإذاعة الان", callback_data="broadcast_confirm"),
                InlineKeyboardButton("❌ إلغاء الإذاعة", callback_data="broadcast_cancel")
            ]
        ])

        await message.reply_text(
            "📢 **معاينة الإذاعة:**\n"
            "هذه هي الرسالة التي سيتم إرسالها لكافة مستخدمي البوت.\n"
            "هل ترغب بالبدء بالإذاعة فوراً؟",
            reply_to_message_id=message.id,
            reply_markup=buttons
        )

    # 5. حالة البحث عن ملف بـ ID
    elif action == "waiting_search_file_id":
        ADMIN_STATES.pop(user_id, None)
        try:
            f_id = int(message.text.strip())
            file_row = db.get_file(f_id)
            if not file_row:
                await message.reply_text(
                    f"❌ لم يتم العثور على ملف بالرقم `{f_id}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 إدارة الملفات", callback_data="admin_files:0")]])
                )
                return

            storage_message_id, caption, downloads, created_at = file_row
            link = make_link(f_id)
            info_text = (
                f"📄 **تفاصيل الملف #{f_id}:**\n\n"
                f"📥 عدد التحميلات: `{downloads}`\n"
                f"📅 تاريخ الإضافة: `{created_at}`\n"
                f"📝 الوصف: {caption or 'بدون وصف'}\n\n"
                f"🔗 الرابط:\n{link}"
            )
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 حذف الملف", callback_data=f"file_delete:{f_id}")],
                [InlineKeyboardButton("📁 إدارة الملفات", callback_data="admin_files:0")]
            ])
            await message.reply_text(info_text, reply_markup=buttons)

        except (ValueError, AttributeError):
            await message.reply_text(
                "❌ يرجى إدخال رقم ID صحيح (أرقام فقط).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 إدارة الملفات", callback_data="admin_files:0")]])
            )


# ---------------------------------------------------------------------------
# وظائف معاينة ونشر المنشورات بواسطة الـ ID
# ---------------------------------------------------------------------------

async def preview_post_by_id(client: Client, user_id: int, post_id: int):
    post_row = db.get_post(post_id)
    if not post_row:
        await client.send_message(user_id, f"❌ لم يتم العثور على منشور بالرقم `{post_id}`")
        return

    post_chat_id, post_message_id, file_id, extra_buttons, created_at = post_row
    download_link = make_link(file_id)
    keyboard = build_post_keyboard(download_link, extra_buttons)

    await client.send_message(user_id, f"📌 **معاينة المنشور رقم #{post_id}:**")
    await client.copy_message(
        chat_id=user_id,
        from_chat_id=post_chat_id,
        message_id=post_message_id,
        reply_markup=keyboard
    )

    action_buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 نشر المنشور #{post_id} في القنوات", callback_data=f"post_publish_saved:{post_id}")],
        [InlineKeyboardButton("📚 قائمة المنشورات", callback_data="admin_posts:0")]
    ])
    await client.send_message(
        user_id,
        f"💡 لنشر هذا المنشور أرسل: `ارسال {post_id}` أو اضغط الزر أدناه:",
        reply_markup=action_buttons
    )


async def publish_post_by_id(client: Client, user_id: int, post_id: int):
    post_row = db.get_post(post_id)
    if not post_row:
        await client.send_message(user_id, f"❌ لم يتم العثور على منشور بالرقم `{post_id}`")
        return

    post_chat_id, post_message_id, file_id, extra_buttons, created_at = post_row
    public_channels = db.get_public_channels()

    if not public_channels:
        await client.send_message(
            user_id,
            "⚠️ لا توجد قنوات نشر مضافة! أضف قنوات نشر من لوحة التحكم أولاً.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 قنوات النشر", callback_data="admin_public_channels")]])
        )
        return

    download_link = make_link(file_id)
    keyboard = build_post_keyboard(download_link, extra_buttons)

    success_cnt = 0
    failed_cnt = 0

    for ch in public_channels:
        try:
            ch_target = int(ch) if (ch.startswith("-100") or ch.isdigit()) else ch
            await client.copy_message(
                chat_id=ch_target,
                from_chat_id=post_chat_id,
                message_id=post_message_id,
                reply_markup=keyboard
            )
            success_cnt += 1
        except Exception as e:
            logger.error(f"Failed to publish post #{post_id} to {ch}: {e}")
            failed_cnt += 1

    report = (
        f"🚀 **تم نشر المنشور رقم #{post_id} بنجاح!**\n\n"
        f"✅ تم النشر في: `{success_cnt}` قناة\n"
        f"❌ تعذر النشر في: `{failed_cnt}` قناة"
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")]])
    await client.send_message(user_id, report, reply_markup=buttons)


# ---------------------------------------------------------------------------
# معالجة أزرار لوحة التحكم والتدفقات (Callback Queries)
# ---------------------------------------------------------------------------

@app.on_callback_query(filters.user(config.ADMIN_IDS))
async def admin_callbacks(client: Client, callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id

    # إلغاء الحالات إذا لم تكن تفاعلية تابعة لعملية النشر أو الإذاعة الحالية
    if not data.startswith("broadcast_confirm") and not data.startswith("post_publish_confirm") and not data.startswith("post_"):
        if data not in ("admin_broadcast_prompt", "admin_create_post_prompt"):
            ADMIN_STATES.pop(user_id, None)

    # 1. اللوحة الرئيسية
    if data == "admin_panel":
        await callback.message.edit_text(
            "🎛 **لوحة تحكم الأدمن**\n\n"
            "مرحباً بك! اختر من الأزرار الشفافة أدناه للتحكم بكافة إعدادات البوت.",
            reply_markup=get_admin_main_keyboard()
        )

    # 2. إنشاء منشور تطبيق
    elif data == "admin_create_post_prompt":
        ADMIN_STATES[user_id] = {"action": "waiting_post_content"}
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="post_cancel")]
        ])
        await callback.message.edit_text(
            "📝 **إنشاء منشور تطبيق جديد:**\n\n"
            "قم بإرسال أو توجيه **المنشور الرئيسي** الآن (صورة مع شرح التطبيق، أو نص، أو فيديو).\n"
            "بعد إرساله سيطلب منك البوت إرسال ملف التطبيق لربطه بالمنشور تلقائياً.",
            reply_markup=buttons
        )

    elif data == "post_add_button_prompt":
        state = ADMIN_STATES.get(user_id)
        if state:
            state["action"] = "waiting_button_input"
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ إلغاء والأكتفاء بالحالي", callback_data="post_finish_buttons")]
            ])
            await callback.message.edit_text(
                "➕ **إضافة زر خارجي جديد:**\n\n"
                "أرسل نص الزر ورابطه مفصولين بشرطة `-` بالشكل التالي:\n"
                "`مشاهدة الشرح - https://youtube.com/watch?v=xxx`",
                reply_markup=buttons
            )

    elif data == "post_finish_buttons":
        state = ADMIN_STATES.get(user_id)
        if not state or "file_id" not in state:
            await callback.answer("حدث خطأ في الجلسة", show_alert=True)
            return

        file_id = state["file_id"]
        post_msg_id = state["post_message_id"]
        post_chat_id = state["post_chat_id"]
        extra_buttons = state.get("extra_buttons", [])

        # حفظ المنشور برقم تسلسلي في قاعدة البيانات
        post_id = db.add_post(
            post_chat_id=post_chat_id,
            post_message_id=post_msg_id,
            file_id=file_id,
            extra_buttons=extra_buttons
        )

        download_link = make_link(file_id)
        keyboard = build_post_keyboard(download_link, extra_buttons)

        ADMIN_STATES[user_id] = {
            "action": "confirm_publish_post",
            "post_id": post_id,
            "download_link": download_link
        }

        # إرسال المعاينة النهائية للمنشور
        await client.copy_message(
            chat_id=user_id,
            from_chat_id=post_chat_id,
            message_id=post_msg_id,
            reply_markup=keyboard
        )

        pub_channels = db.get_public_channels()

        action_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🚀 نشر المنشور #{post_id} الآن", callback_data="post_publish_confirm"),
                InlineKeyboardButton("❌ إغلاق", callback_data="post_cancel")
            ]
        ])

        await callback.message.reply_text(
            f"✨ **تم حفظ المنشور بنجاح تحت الرقم: #{post_id}**\n\n"
            f"📌 يمكنك معاينته بأمر: `معاينة {post_id}`\n"
            f"🚀 ويمكنك نشره بأمر: `ارسال {post_id}`\n\n"
            f"📢 عدد قنوات النشر المحددة: `{len(pub_channels)}`\n"
            "هل ترغب بنشره الآن في قنوات النشر العامة؟",
            reply_markup=action_buttons
        )

    elif data == "post_cancel":
        ADMIN_STATES.pop(user_id, None)
        await callback.answer("تم إلغاء المنشور")
        await callback.message.edit_text(
            "❌ تم إلغاء المنشور.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للوحة الرئيسي", callback_data="admin_panel")]])
        )

    elif data == "post_publish_confirm":
        state = ADMIN_STATES.pop(user_id, None)
        if not state or "post_id" not in state:
            await callback.answer("انتهت صلاحية الجلسة أو حدث خطأ.", show_alert=True)
            return

        post_id = state["post_id"]
        await publish_post_by_id(client, user_id, post_id)

    elif data.startswith("post_publish_saved:"):
        p_id = int(data.split("post_publish_saved:", 1)[1])
        await publish_post_by_id(client, user_id, p_id)

    # 3. المنشورات المحفوظة
    elif data.startswith("admin_posts:"):
        page = int(data.split("admin_posts:", 1)[1])
        limit = 5
        offset = page * limit
        posts_list = db.get_recent_posts(limit=limit, offset=offset)
        total_posts = db.get_posts_count()

        text = f"📚 **قائمة المنشورات المحفوظة** (الصفحة {page + 1}):\n\n"
        buttons = []

        if not posts_list:
            text += "لا توجد منشورات محفوظة حالياً."
        else:
            for p_id, p_chat_id, p_msg_id, f_id, created_at in posts_list:
                buttons.append([
                    InlineKeyboardButton(f"📌 منشور #{p_id} (ملف #{f_id}) - {created_at[:10]}", callback_data=f"post_info:{p_id}")
                ])

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ السابقة", callback_data=f"admin_posts:{page - 1}"))
        if offset + limit < total_posts:
            nav_buttons.append(InlineKeyboardButton("التالية ▶️", callback_data=f"admin_posts:{page + 1}"))

        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")])

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("post_info:"):
        p_id = int(data.split("post_info:", 1)[1])
        await preview_post_by_id(client, user_id, p_id)

    # 4. الأعلى تحميلاً (> 10 مرات)
    elif data == "admin_top_downloads":
        top_files = db.get_top_downloaded_files(min_downloads=10, limit=20)

        text = "🔥 **قائمة أكثر التطبيقات تحميلاً (أكثر من 10 مرات):**\n\n"
        buttons = []

        if not top_files:
            text += "لا توجد تطبيقات بلغت أكثر من 10 تحميلات حتى الآن."
        else:
            for f_id, s_msg_id, cap, downloads, created_at in top_files:
                short_cap = (cap[:20] + "...") if cap else "بدون عنوان"
                buttons.append([
                    InlineKeyboardButton(f"🔥 #{f_id} | {short_cap} ({downloads} 📥)", callback_data=f"file_info:{f_id}")
                ])

        buttons.append([InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")])
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    # 5. إدارة قنوات النشر العامة
    elif data == "admin_public_channels":
        channels = db.get_public_channels()
        channels_str = "\n".join([f"• `{ch}`" for ch in channels]) if channels else "لا توجد قنوات نشر مضافة"

        text = (
            "📢 **إدارة قنوات النشر العامة**\n\n"
            "هذه هي القنوات التي يتم نشر منشورات التطبيقات فيها تلقائياً مع زر التحميل الشفاف.\n\n"
            f"القنوات الحالية:\n{channels_str}"
        )

        buttons = [
            [InlineKeyboardButton("➕ إضافة قناة نشر جديدة", callback_data="public_add_prompt")],
        ]

        if channels:
            buttons.append([InlineKeyboardButton("🗑 حذف قناة نشر", callback_data="public_manage")])

        buttons.append([InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")])

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "public_add_prompt":
        ADMIN_STATES[user_id] = {"action": "waiting_add_public_channel"}
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_public_channels")]
        ])
        await callback.message.edit_text(
            "➕ **إضافة قناة نشر جديدة:**\n\n"
            "أرسل الآن يوزر القناة (مثل `@mychannel`) أو رابطها أو آيدي القناة.\n"
            "⚠️ ملاحظة: تأكد من رفع البوت أدمن في قناة النشر بصلاحية نشر الرسائل.",
            reply_markup=buttons
        )

    elif data == "public_manage":
        channels = db.get_public_channels()
        if not channels:
            await callback.answer("لا توجد قنوات للحذف", show_alert=True)
            return

        buttons = []
        for ch in channels:
            buttons.append([
                InlineKeyboardButton(f"❌ حذف: {ch}", callback_data=f"public_del:{ch}")
            ])
        buttons.append([InlineKeyboardButton("🔙 العودة", callback_data="admin_public_channels")])

        await callback.message.edit_text(
            "🗑 **اختر قناة النشر التي تريد حذفها:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("public_del:"):
        ch_to_del = data.split("public_del:", 1)[1]
        db.remove_public_channel(ch_to_del)
        await callback.answer("تم حذف قناة النشر بنجاح ✅")
        callback.data = "admin_public_channels"
        await admin_callbacks(client, callback)

    # 6. الإحصائيات
    elif data == "admin_stats":
        users_cnt = db.get_users_count()
        files_cnt = db.get_files_count()
        total_downloads = db.get_total_downloads()
        forcesub_status = "مفعّل ✅" if db.is_force_sub_enabled() else "معطل ❌"
        channels_cnt = len(db.get_force_sub_channels())
        public_channels_cnt = len(db.get_public_channels())
        posts_cnt = db.get_posts_count()

        stats_text = (
            "📊 **إحصائيات البوت الشاملة:**\n\n"
            f"👤 عدد المستخدمين: `{users_cnt}`\n"
            f"📁 عدد الملفات المرفوعة: `{files_cnt}`\n"
            f"📥 إجمالي التحميلات: `{total_downloads}`\n"
            f"📚 عدد المنشورات المحفوظة: `{posts_cnt}`\n"
            f"🔐 حالة الاشتراك الإجباري: {forcesub_status}\n"
            f"🔗 قنوات الاشتراك الإجباري: `{channels_cnt}`\n"
            f"📢 قنوات النشر العامة: `{public_channels_cnt}`\n"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحديث", callback_data="admin_stats")],
            [InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")]
        ])
        await callback.message.edit_text(stats_text, reply_markup=buttons)

    # 7. إدارة الاشتراك الإجباري
    elif data == "admin_forcesub":
        is_enabled = db.is_force_sub_enabled()
        channels = db.get_force_sub_channels()

        status_str = "مفعّل ✅" if is_enabled else "معطل ❌"
        channels_str = "\n".join([f"• `{ch}`" for ch in channels]) if channels else "لا يوجد قنوات مضافة"

        text = (
            "🔐 **إدارة الاشتراك الإجباري**\n\n"
            f"الحالة الحالية: **{status_str}**\n\n"
            f"القنوات الحالية:\n{channels_str}"
        )

        toggle_btn = InlineKeyboardButton(
            "❌ إيقاف الاشتراك" if is_enabled else "✅ تفعيل الاشتراك",
            callback_data="forcesub_toggle"
        )

        buttons = [
            [toggle_btn],
            [InlineKeyboardButton("➕ إضافة قناة جديد", callback_data="forcesub_add_prompt")],
        ]

        if channels:
            buttons.append([InlineKeyboardButton("🗑 إدارة / حذف القنوات", callback_data="forcesub_manage")])

        buttons.append([InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")])

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "forcesub_toggle":
        current = db.is_force_sub_enabled()
        db.set_force_sub_enabled(not current)
        await callback.answer("تم تغيير حالة الاشتراك الإجباري بنجاح")
        callback.data = "admin_forcesub"
        await admin_callbacks(client, callback)

    elif data == "forcesub_add_prompt":
        ADMIN_STATES[user_id] = {"action": "waiting_add_channel"}
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_forcesub")]
        ])
        await callback.message.edit_text(
            "➕ **إضافة قناة اشتراك إجباري:**\n\n"
            "أرسل الآن يوزر القناة (مثل `@mychannel`) أو رابطها أو آيدي القناة.\n"
            "⚠️ ملاحظة: تأكد من رفع البوت أدمن في القناة أولاً.",
            reply_markup=buttons
        )

    elif data == "forcesub_manage":
        channels = db.get_force_sub_channels()
        if not channels:
            await callback.answer("لا توجد قنوات للحذف", show_alert=True)
            return

        buttons = []
        for ch in channels:
            buttons.append([
                InlineKeyboardButton(f"❌ حذف: {ch}", callback_data=f"forcesub_del:{ch}")
            ])
        buttons.append([InlineKeyboardButton("🔙 العودة", callback_data="admin_forcesub")])

        await callback.message.edit_text(
            "🗑 **اختر القناة التي تريد حذفها من الاشتراك الإجباري:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("forcesub_del:"):
        ch_to_del = data.split("forcesub_del:", 1)[1]
        db.remove_force_sub_channel(ch_to_del)
        await callback.answer("تم حذف القناة بنجاح ✅")
        callback.data = "admin_forcesub"
        await admin_callbacks(client, callback)

    # 8. الإذاعة
    elif data == "admin_broadcast_prompt":
        ADMIN_STATES[user_id] = {"action": "waiting_broadcast_msg"}
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]
        ])
        await callback.message.edit_text(
            "📢 **قسم الإذاعة العامة:**\n\n"
            "قم بإرسال أو توجيه الرسالة التي تريد إذاعتها لجميع المستخدمين الآن.\n"
            "(تقبل الإذاعة: النصوص، الصور، الفيديو، الصوت، والرسائل الموجهة).",
            reply_markup=buttons
        )

    elif data == "broadcast_cancel":
        ADMIN_STATES.pop(user_id, None)
        await callback.answer("تم إلغاء الإذاعة")
        await callback.message.edit_text(
            "❌ تم إلغاء الإذاعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للوحة", callback_data="admin_panel")]])
        )

    elif data == "broadcast_confirm":
        state = ADMIN_STATES.pop(user_id, None)
        if not state or state.get("action") != "confirm_broadcast":
            await callback.answer("حدث خطأ أو انتهت صلاحية الإذاعة.", show_alert=True)
            return

        msg_id = state["message_id"]
        from_chat = state["chat_id"]
        all_users = db.get_all_user_ids()

        await callback.message.edit_text(f"⏳ جاري بدء الإذاعة لـ `{len(all_users)}` مستخدم...")

        success = 0
        failed = 0

        for u_id in all_users:
            try:
                await client.copy_message(
                    chat_id=u_id,
                    from_chat_id=from_chat,
                    message_id=msg_id
                )
                success += 1
            except FloodWait as e:
                await asyncio.sleep(e.value)
                try:
                    await client.copy_message(chat_id=u_id, from_chat_id=from_chat, message_id=msg_id)
                    success += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1

        report = (
            "✅ **تمت الإذاعة بنجاح!**\n\n"
            f"👥 إجمالي المستخدمين: `{len(all_users)}`\n"
            f"✅ تم الإرسال بنجاح: `{success}`\n"
            f"❌ فشل الإرسال (حظر/حساب مغلق): `{failed}`"
        )
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للوحة", callback_data="admin_panel")]])
        await callback.message.edit_text(report, reply_markup=buttons)

    # 9. إدارة الملفات
    elif data.startswith("admin_files:"):
        page = int(data.split("admin_files:", 1)[1])
        limit = 5
        offset = page * limit
        files_list = db.get_recent_files(limit=limit, offset=offset)
        total_files = db.get_files_count()

        text = f"📁 **قائمة الملفات المرفوعة** (الصفحة {page + 1}):\n\n"
        buttons = []

        if not files_list:
            text += "لا توجد ملفات مرفوعة حالياً."
        else:
            for f_id, s_msg_id, cap, downloads, created_at in files_list:
                short_cap = (cap[:20] + "...") if cap else "بدون عنوان"
                buttons.append([
                    InlineKeyboardButton(f"📄 #{f_id} | {short_cap} ({downloads} 📥)", callback_data=f"file_info:{f_id}")
                ])

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ السابقة", callback_data=f"admin_files:{page - 1}"))
        if offset + limit < total_files:
            nav_buttons.append(InlineKeyboardButton("التالية ▶️", callback_data=f"admin_files:{page + 1}"))

        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([InlineKeyboardButton("🔍 بحث عن ملف بـ ID", callback_data="file_search_prompt")])
        buttons.append([InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")])

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("file_info:"):
        f_id = int(data.split("file_info:", 1)[1])
        file_row = db.get_file(f_id)
        if not file_row:
            await callback.answer("الملف غير موجود!", show_alert=True)
            return

        storage_message_id, caption, downloads, created_at = file_row
        link = make_link(f_id)
        info_text = (
            f"📄 **تفاصيل الملف #{f_id}:**\n\n"
            f"📥 عدد التحميلات: `{downloads}`\n"
            f"📅 تاريخ الإضافة: `{created_at}`\n"
            f"📝 الوصف: {caption or 'بدون وصف'}\n\n"
            f"🔗 الرابط المباشر:\n{link}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 حذف الملف", callback_data=f"file_delete:{f_id}")],
            [InlineKeyboardButton("📁 إدارة الملفات", callback_data="admin_files:0")]
        ])
        await callback.message.edit_text(info_text, reply_markup=buttons)

    elif data.startswith("file_delete:"):
        f_id = int(data.split("file_delete:", 1)[1])
        db.delete_file(f_id)
        await callback.answer("تم حذف الملف بنجاح ✅", show_alert=True)
        callback.data = "admin_files:0"
        await admin_callbacks(client, callback)

    elif data == "file_search_prompt":
        ADMIN_STATES[user_id] = {"action": "waiting_search_file_id"}
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="admin_files:0")]
        ])
        await callback.message.edit_text(
            "🔍 **البحث عن ملف:**\n\n"
            "أرسل الآن رقم (ID) الملف للبحث عنه والاطلاع على إحصائياته أو حذفه.",
            reply_markup=buttons
        )

    # 10. إغلاق اللوحة
    elif data == "admin_close":
        await callback.message.delete()


if __name__ == "__main__":
    db.init_db()
    logger.info("Bot is starting...")
    app.run()
