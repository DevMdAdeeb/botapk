import logging
from typing import Dict, Any

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

# القاموس المخصص لإدارة حالات الأدمن التفاوضية (Waiting state)
# user_states[user_id] = {"action": "waiting_...", ...}
ADMIN_STATES: Dict[int, Dict[str, Any]] = {}


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def make_link(file_id: int) -> str:
    return f"https://t.me/{config.BOT_USERNAME}?start=file_{file_id}"


# ---------------------------------------------------------------------------
# زر ولوحة التحكم للآدمن
# ---------------------------------------------------------------------------

def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
            InlineKeyboardButton("📢 الإذاعة", callback_data="admin_broadcast_prompt"),
        ],
        [
            InlineKeyboardButton("📢 الاشتراك الإجباري", callback_data="admin_forcesub"),
            InlineKeyboardButton("📁 إدارة الملفات", callback_data="admin_files:0"),
        ],
        [
            InlineKeyboardButton("❌ إغلاق اللوحة", callback_data="admin_close")
        ]
    ])


async def send_admin_panel(client: Client, chat_id: int):
    stats_text = (
        "🎛 **لوحة تحكم الأدمن**\n\n"
        "مرحباً بك! اختر من الأزرار الشفافة أدناه للتحكم بكافة إعدادات البوت."
    )
    await client.send_message(
        chat_id=chat_id,
        text=stats_text,
        reply_markup=get_admin_main_keyboard()
    )


# ---------------------------------------------------------------------------
# التفاعل مع الرسائل الواردة (/start وغيره)
# ---------------------------------------------------------------------------

@app.on_message(filters.private & filters.command("start"))
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or ""
    username = message.from_user.username or ""

    # تسجيل المستخدم في قاعدة البيانات
    db.add_user(user_id, first_name, username)

    args = message.command

    # دخول عبر رابط ملف
    if len(args) >= 2 and args[1].startswith("file_"):
        param = args[1]
        try:
            file_id = int(param.replace("file_", "", 1))
        except ValueError:
            await message.reply_text("❌ الرابط غير صالح.")
            return

        await deliver_file(client, user_id, file_id, message)
        return

    # دخول عادي بدون رابط ملف
    if is_admin(user_id):
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎛 فتح لوحة التحكم", callback_data="admin_panel")]
        ])
        await message.reply_text(
            "أهلاً بك يا مدير 👋\n"
            "اضغط على الزر أدناه للوصول إلى لوحة التحكم الشفافة.",
            reply_markup=buttons
        )
    else:
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
        # التعامل مع المعرفات الرقمية أو اليوزرنيم
        ch_target = int(channel) if (channel.startswith("-100") or channel.isdigit()) else channel
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


async def ask_to_subscribe(client: Client, chat_id: int, channels: list, file_id: int):
    buttons = []
    for idx, ch in enumerate(channels, 1):
        if ch.startswith("http://") or ch.startswith("https://"):
            display_link = ch
        elif ch.startswith("@"):
            display_link = f"https://t.me/{ch.lstrip('@')}"
        elif not ch.startswith("-100"):
            display_link = f"https://t.me/{ch}"
        else:
            display_link = None

        if display_link:
            buttons.append([InlineKeyboardButton(f"📢 اشترك في القناة ({idx})", url=display_link)])

    buttons.append(
        [InlineKeyboardButton("✅ تحققت، أعطني الملف", callback_data=f"check_sub:{file_id}")]
    )

    await client.send_message(
        chat_id,
        "⚠️ يجب عليك الاشتراك في القنوات التالية أولًا لاستلام الملف:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@app.on_callback_query(filters.regex(r"^check_sub:(\d+)$"))
async def check_sub_callback(client: Client, callback: CallbackQuery):
    file_id = int(callback.matches[0].group(1))
    user_id = callback.from_user.id

    if not db.is_force_sub_enabled():
        await callback.message.delete()
        await deliver_file(client, user_id, file_id, callback.message)
        return

    channels = db.get_force_sub_channels()
    unsubscribed = []
    for ch in channels:
        if not await check_subscription(client, user_id, ch):
            unsubscribed.append(ch)

    if not unsubscribed:
        await callback.answer("تم التحقق بنجاح ✅")
        await callback.message.delete()
        await deliver_file(client, user_id, file_id, callback.message)
    else:
        await callback.answer("❌ لم يتم رصد اشتراكك في جميع القنوات بعد!", show_alert=True)


# ---------------------------------------------------------------------------
# رفع الملفات من الأدمن
# ---------------------------------------------------------------------------

@app.on_message(filters.private & MEDIA_FILTER & filters.user(config.ADMIN_IDS))
async def admin_upload_handler(client: Client, message: Message):
    user_id = message.from_user.id

    # إذا كان الأدمن في حالة انتظار أخرى (مثل الإذاعة) لا يعتبر هذا رفع ملفات
    if user_id in ADMIN_STATES:
        await process_admin_state_input(client, message)
        return

    caption = message.caption or ""

    # نسخ الملف إلى قناة التخزين الخاصة
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


# ---------------------------------------------------------------------------
# كود الاستجابة للرسائل النصية والمدخلات التفاعلية للأدمن
# ---------------------------------------------------------------------------

@app.on_message(filters.private & ~filters.command("start") & filters.user(config.ADMIN_IDS))
async def admin_message_router(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in ADMIN_STATES:
        await process_admin_state_input(client, message)
    else:
        # إذا أرسل الأدمن رسالة عادية غير معرفة، نعرض له زر لوحة التحكم
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎛 فتح لوحة التحكم", callback_data="admin_panel")]
        ])
        await message.reply_text("🎛 يمكنك التحكم بالبوت عبر لوحة التحكم الشفافة:", reply_markup=buttons)


async def process_admin_state_input(client: Client, message: Message):
    user_id = message.from_user.id
    state_info = ADMIN_STATES.get(user_id)
    if not state_info:
        return

    action = state_info.get("action")

    # 1. حالة إضافة قناة اشتراك إجباري
    if action == "waiting_add_channel":
        channel_text = message.text.strip() if message.text else ""
        if not channel_text:
            await message.reply_text("❌ يرجى إرسال يوزر القناة (مثال: `@mychannel`) أو رابطها أو آيدي القناة.")
            return

        success = db.add_force_sub_channel(channel_text)
        ADMIN_STATES.pop(user_id, None)

        if success:
            await message.reply_text(
                f"✅ تم إضافة القناة `{channel_text}` بنجاح!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="admin_forcesub")]])
            )
        else:
            await message.reply_text(
                f"⚠️ القناة `{channel_text}` مضافة بالفعل سابقاً.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="admin_forcesub")]])
            )

    # 2. حالة تجهيز الإذاعة
    elif action == "waiting_broadcast_msg":
        # تخزين الرسالة وتأكيد الإذاعة
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

    # 3. حالة البحث عن ملف بـ ID
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
# معالجة أزرار لوحة التحكم (Callback Queries)
# ---------------------------------------------------------------------------

@app.on_callback_query(filters.user(config.ADMIN_IDS))
async def admin_callbacks(client: Client, callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id

    # إلغاء أية حالة سابقة عند النقر على أي زر لوحة تحكم
    if not data.startswith("broadcast_confirm"):
        if data != "admin_broadcast_prompt":
            ADMIN_STATES.pop(user_id, None)

    # 1. اللوحة الرئيسية
    if data == "admin_panel":
        await callback.message.edit_text(
            "🎛 **لوحة تحكم الأدمن**\n\n"
            "مرحباً بك! اختر من الأزرار الشفافة أدناه للتحكم بكافة إعدادات البوت.",
            reply_markup=get_admin_main_keyboard()
        )

    # 2. الإحصائيات
    elif data == "admin_stats":
        users_cnt = db.get_users_count()
        files_cnt = db.get_files_count()
        total_downloads = db.get_total_downloads()
        forcesub_status = "مفعّل ✅" if db.is_force_sub_enabled() else "معطل ❌"
        channels_cnt = len(db.get_force_sub_channels())

        stats_text = (
            "📊 **إحصائيات البوت الشاملة:**\n\n"
            f"👤 عدد المستخدمين: `{users_cnt}`\n"
            f"📁 عدد الملفات المرفوعة: `{files_cnt}`\n"
            f"📥 إجمالي التحميلات: `{total_downloads}`\n"
            f"📢 حالة الاشتراك الإجباري: {forcesub_status}\n"
            f"🔗 عدد قنوات الاشتراك: `{channels_cnt}`\n"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحديث", callback_data="admin_stats")],
            [InlineKeyboardButton("🔙 العودة للوحة الرئيسية", callback_data="admin_panel")]
        ])
        await callback.message.edit_text(stats_text, reply_markup=buttons)

    # 3. إدارة الاشتراك الإجباري
    elif data == "admin_forcesub":
        is_enabled = db.is_force_sub_enabled()
        channels = db.get_force_sub_channels()

        status_str = "مفعّل ✅" if is_enabled else "معطل ❌"
        channels_str = "\n".join([f"• `{ch}`" for ch in channels]) if channels else "لا يوجد قنوات مضافة"

        text = (
            "📢 **إدارة الاشتراك الإجباري**\n\n"
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
        # إعادة توجيه لنفس القائمة
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

    # 4. الإذاعة
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
                import asyncio
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

    # 5. إدارة الملفات
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

    # 6. إغلاق اللوحة
    elif data == "admin_close":
        await callback.message.delete()


if __name__ == "__main__":
    db.init_db()
    logger.info("Bot is starting...")
    app.run()
