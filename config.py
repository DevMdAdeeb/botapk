import os
from dotenv import load_dotenv

load_dotenv()

# بيانات API من my.telegram.org
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

# توكن البوت من BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# آيدي الأدمن (رقمك الشخصي على تيليجرام) - يمكن وضع أكثر من آيدي مفصولة بفاصلة
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# آيدي قناة التخزين الخاصة (يجب أن يكون البوت أدمن فيها)
# مثال: -1001234567890
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))

# اسم يوزر البوت بدون @ (يُستخدم لتوليد الروابط)
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# مسار قاعدة البيانات
DB_PATH = os.getenv("DB_PATH", "filestore.db")
