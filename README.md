# بوت تخزين وتوزيع التطبيقات (File Store Bot)

## الخطوات

### 1. الحصول على البيانات المطلوبة
- `API_ID` و `API_HASH`: من https://my.telegram.org (Apps section)
- `BOT_TOKEN`: أنشئ بوتًا عبر [@BotFather](https://t.me/BotFather) وخذ التوكن
- `ADMIN_IDS`: آيدي حسابك، احصل عليه من [@userinfobot](https://t.me/userinfobot)
- `STORAGE_CHANNEL_ID`:
  1. أنشئ قناة خاصة جديدة (لن يراها أحد سوى البوت)
  2. أضف بوتك كـ **أدمن** فيها (بصلاحية إرسال الرسائل)
  3. أرسل أي رسالة في القناة، ثم قم بعمل Forward لها إلى [@JsonDumpBot](https://t.me/JsonDumpBot) لتحصل على `chat.id` (يبدأ بـ `-100`)
- `BOT_USERNAME`: يوزر بوتك بدون @

### 2. التثبيت على الـ VPS

```bash
# رفع المجلد إلى السيرفر (مثلاً عبر scp أو git)
cd /root/filestore_bot

# تثبيت بايثون والمكتبات
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# إعداد بيانات الاعتماد
cp .env.example .env
nano .env   # عدّل القيم بالبيانات الحقيقية
```

### 3. التشغيل التجريبي

```bash
python3 bot.py
```

إذا ظهرت رسالة "Bot is starting..." بدون أخطاء، فالبوت يعمل بنجاح. جرّب إرسال ملف له من حساب الأدمن.

### 4. التشغيل الدائم عبر systemd (يبقى يعمل حتى بعد إغلاق الطرفية أو إعادة تشغيل السيرفر)

```bash
sudo cp filestorebot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable filestorebot
sudo systemctl start filestorebot

# لمتابعة السجلات
sudo journalctl -u filestorebot -f
```

## طريقة الاستخدام

### كأدمن:
1. أرسل أي ملف (برنامج) للبوت في الخاص.
2. سيرد عليك برابط جاهز مباشرة مثل:
   `https://t.me/YourBot?start=file_5`
3. انسخ هذا الرابط وضعه في منشورك على قناة التطبيقات.

### أوامر الأدمن:
| الأمر | الوظيفة |
|---|---|
| `/forcesub on` | تفعيل الاشتراك الإجباري |
| `/forcesub off` | إيقاف الاشتراك الإجباري |
| `/setchannel @username` أو `/setchannel -100xxxx` | تحديد قناة الاشتراك الإجباري |
| `/status` | عرض الحالة الحالية للإعدادات |

### للمستخدم العادي:
يضغط على الرابط → يدخل للبوت → يستلم الملف تلقائيًا (أو يُطلب منه الاشتراك أولًا إن كانت الميزة مفعّلة).

## ملاحظات مهمة
- تأكد أن البوت **أدمن** في قناة التخزين الخاصة، وإلا لن يستطيع نسخ الملفات منها لاحقًا.
- إذا فعّلت الاشتراك الإجباري، يجب أن يكون البوت **أدمن** أيضًا في قناة الاشتراك (وإلا لن يستطيع التحقق من العضوية).
- قاعدة البيانات (`filestore.db`) تُنشأ تلقائيًا بجانب `bot.py` عند أول تشغيل.
- يمكنك عمل نسخة احتياطية من `filestore.db` بشكل دوري للحفاظ على الروابط.
