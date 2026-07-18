# نقشهٔ راه پیشنهادی محصول

> سامانهٔ مدیریت نمایندگان، فاکتورها و فروشگاه Hiddify
>
> تاریخ تدوین: ۱۴۰۵/۰۴/۲۵ — 2026-07-16  
> مبنای بررسی: commit cf80dfd  
> وضعیت سند: مورد ۱ در نسخهٔ v1.82.3 تکمیل شده است؛ سایر موارد منتظر انتخاب هستند.

## هدف سند

این فایل ۲۰ پیشنهاد محصولی و فنیِ مبتنی بر ساختار فعلی پروژه را ثبت می‌کند تا مالک
پروژه بتواند موارد موردنظر را انتخاب کند. پس از انتخاب، برای هر مورد یک برنامهٔ اجرایی
مستقل شامل scope دقیق، فایل‌های درگیر، migration، تست‌ها، ترتیب Release و شرایط توقف
تهیه می‌شود.

اصل راهبردی این roadmap:

> ربات باید درگاه ورود، اعلان و عملیات سریع باشد؛ پورتال باید محل اصلی مدیریت باشد.

شماره‌ها شناسهٔ ثابت پیشنهادها هستند و در این مرحله به‌معنای ترتیب قطعی اجرا نیستند.

## راهنمای وضعیت

| وضعیت | معنی |
|---|---|
| CANDIDATE | پیشنهاد بررسی‌شده ولی هنوز انتخاب نشده |
| SELECTED | مالک پروژه آن را برای اجرا انتخاب کرده |
| PLANNED | برنامهٔ اجرایی دقیق آن نوشته شده؛ وضعیت تأیید در بخش همان مورد ثبت می‌شود |
| IN PROGRESS | پیاده‌سازی در حال انجام است |
| DONE | تست، Release، deploy و smoke-check کامل شده |
| BLOCKED | مانع مشخصی برای ادامه وجود دارد |
| REJECTED | از roadmap خارج شده و دلیل آن ثبت شده است |

## راهنمای اندازه

| اندازه | تخمین تقریبی |
|---|---|
| XS | کمتر از یک روز |
| S | حدود ۱ تا ۳ روز |
| M | حدود ۴ تا ۸ روز |
| L | چند هفته یا چند Release |
| XL | پروژهٔ چندمرحله‌ای و چندنسخه‌ای |

این تخمین‌ها برای مقایسه‌اند و فقط پس از نوشتن برنامهٔ اجرایی هر مورد دقیق می‌شوند.

## جدول انتخاب

| شماره | عنوان | اندازه | ریسک | Migration | وضعیت |
|---:|---|:---:|:---:|:---:|:---:|
| ۱ | ورود مستقیم و یک‌کلیکی به پورتال | S | کم | خیر | DONE |
| ۲ | ساخت کاربر از داخل پورتال نماینده | M | متوسط | احتمالاً خیر | REJECTED |
| ۳ | مرکز کامل مدیریت فروشگاه در پورتال | XL | متوسط/زیاد | بله | DONE (v1.88.0) |
| ۴ | فاکتور و وصول مطالبات نماینده از زیرمجموعه‌ها | XL | زیاد | بله | CANDIDATE |
| ۵ | بازطراحی کامل منوی ربات‌ها | M | متوسط | خیر | DONE (v1.89.0) |
| ۶ | سیستم پایدار تیکت، درخواست و مکالمه | L | متوسط | بله | CANDIDATE |
| ۷ | صندوق عملیات یکپارچه برای مالک | M/L | متوسط | وابسته به ۶ | CANDIDATE |
| ۸ | درخت کامل زیرمجموعه‌ها و عملیات گروهی | M/L | متوسط | احتمالاً خیر | CANDIDATE |
| ۹ | Mini App یا پورتال مشتریان فروشگاه | XL | زیاد | احتمالاً بله | CANDIDATE |
| ۱۰ | تمدید خودکار سرویس از کیف پول | M/L | زیاد | بله | CANDIDATE |
| ۱۱ | تطبیق و تأیید خودکار پرداخت رمزارزی | L | زیاد | بله | CANDIDATE |
| ۱۲ | داشبورد سود و زیان واقعی فروشگاه | M/L | متوسط | شاید | CANDIDATE |
| ۱۳ | محافظ قیمت و حاشیه سود پلن‌ها | M | متوسط | احتمالاً بله | CANDIDATE |
| ۱۴ | CRM و اتوماسیون بازاریابی فروشگاه | L | متوسط | بله | CANDIDATE |
| ۱۵ | نقش‌ها و دسترسی تیمی | L | زیاد | بله | CANDIDATE |
| ۱۶ | پیش‌نمایش دقیق enforcement | M | متوسط | احتمالاً خیر | CANDIDATE |
| ۱۷ | Audit Log جامع و تغییرناپذیر | M/L | متوسط | بله | CANDIDATE |
| ۱۸ | خروجی حسابداری و مرکز تسویه | M | کم/متوسط | معمولاً خیر | CANDIDATE |
| ۱۹ | پیش‌بینی پایان ماه و هشدار ناهنجاری | M/L | متوسط | شاید | CANDIDATE |
| ۲۰ | White-label کامل پورتال و فروشگاه | M تا XL | متوسط | بله | CANDIDATE |

---

## ۱. ورود مستقیم و یک‌کلیکی به پورتال

### مسئلهٔ فعلی

دکمهٔ «ورود به پنل تحت وب» در منوی ربات یک callback معمولی است. کاربر آن را می‌زند،
سپس ربات پیام دیگری حاوی لینک ورود ۱۵ دقیقه‌ای می‌فرستد. بنابراین ورود حداقل دو تعامل
می‌خواهد و پیام‌های حاوی لینک موقت نیز در چت باقی می‌مانند.

شواهد فعلی:

- backend/app/bot/keyboards.py:72
- backend/app/bot/handlers/views.py:487

### خروجی نهایی مورد انتظار

کاربر با لمس دکمهٔ پورتال، مستقیماً وارد پورتال شود؛ بدون پیام میانی، کپی URL، نام
کاربری یا رمز عبور. طبق تصمیم مالک محصول، مقصد باید یک URL عادی HTTPS باشد که با
مرورگر تنظیم‌شده در Telegram باز می‌شود؛ Mini App یا Web App در این مورد مجاز نیست.

### محدودهٔ MVP

- تبدیل دکمهٔ فعلی به دکمهٔ URL امن (بدون Mini App/Web App)
- اعتبارسنجی هویت Telegram در backend
- ورود مستقیم به حساب‌های متعلق به همان Telegram user
- نگهداری لینک یک‌بارمصرف فعلی به‌عنوان fallback
- حفظ callback فعلی به‌عنوان fallback در صورت نبود حساب نماینده یا دامنه
- تولید token کوتاه‌عمر فقط هنگام ساخت منوی inline و عدم ذخیره یا ثبت آن در log
- هدایت به صفحهٔ موردنظر از طریق deep link، نه همیشه داشبورد

### نسخه‌های بعدی

- دکمهٔ دائمی «بازکردن پورتال» در menu button خود Telegram
- deep link مستقیم از اعلان‌ها به فاکتور، پرداخت، مشتری یا تیکت
- صفحهٔ خطای اختصاصی برای لینک منقضی‌شده و درخواست سریع منوی تازه

### ریسک‌ها و ملاحظات

- یک URL دارای token پانزده‌دقیقه‌ای نباید در reply keyboard دائمی ذخیره شود؛ دکمه پس
  از انقضا خراب خواهد شد.
- دادهٔ هویتی Telegram باید در backend و با امضای معتبر بررسی شود.
- ورود باید همچنان به policy سخت‌گیرانهٔ HTTPS فعلی پایبند بماند.
- forwarded button یا URL نباید حساب شخص دیگری را باز کند.

### معیارهای پذیرش

- [x] لمس دکمه با یک مرحله پورتال را باز کند.
- [x] کاربر بدون password وارد حساب خودش شود.
- [x] replay توکن ورود یک‌بارمصرف رد شود.
- [x] fallback لینک یک‌بارمصرف همچنان کار کند.
- [x] نبود حساب نماینده یا دامنه، کاربر را به callback توضیحی قبلی هدایت کند.
- [x] تست HTTPS، replay، tenant isolation، expiry و قرارداد دکمه اجرا شود.

### مشخصات تصمیم

- اندازه: S
- ریسک: کم
- Migration: ندارد
- وابستگی: دامنه و HTTPS سالم
- وضعیت: DONE در v1.82.3
- برنامهٔ اجرایی: `plans/001-direct-browser-portal-login.md`
- نتیجهٔ پیاده‌سازی: commit بازبینی‌شدهٔ `f7770a2` روی شاخهٔ
  `advisor/001-direct-browser-portal-login`
- نتیجهٔ Release: ادغام در `main`، انتشار v1.82.3 و smoke-check تولید

---

## ۲. ساخت کاربر از داخل پورتال نماینده

### مسئلهٔ فعلی

ساخت user فقط در ربات و به‌صورت FSM چندمرحله‌ای انجام می‌شود. service اصلی backend
وجود دارد، ولی portal API و رابط وب آن را ارائه نمی‌کنند. برای ساخت گروهی، مشاهدهٔ
نتیجه، کپی لینک‌ها و مدیریت خطای جزئی، رابط وب مناسب‌تر از chat است.

شواهد فعلی:

- backend/app/bot/handlers/views.py:37
- backend/app/services/usercreate.py
- frontend/src/portal/PortalApp.tsx:42

### خروجی نهایی مورد انتظار

صفحه‌ای با عنوان «ساخت سرویس» در پورتال نماینده که ساخت تکی و گروهی را با wizard
واضح انجام دهد.

### جریان پیشنهادی

۱. انتخاب حساب و پنل  
۲. انتخاب ساخت تکی یا گروهی  
۳. تعیین تعداد  
۴. تعیین حجم و مدت  
۵. تعیین نام پایه  
۶. نمایش ظرفیت باقی‌مانده و خلاصهٔ عملیات  
۷. تأیید نهایی  
۸. نمایش نتیجه، لینک‌ها و QRها

### قابلیت‌های MVP

- ساخت تکی و گروهی
- نمایش ظرفیت قبل از اجرا
- محدودکردن گزینه‌ها به تنظیمات مجاز owner
- نتیجهٔ جزئی: موفق، ناموفق و دلیل
- کپی یک لینک یا همهٔ لینک‌ها
- دانلود CSV خروجی
- نمایش QR هر سرویس
- idempotency برای double-click و refresh
- عدم نگه‌داشتن DB session هنگام panel I/O
- استفاده از همان service layer ربات

### نسخه‌های بعدی

- دانلود ZIP شامل QRها
- template نام‌گذاری
- presetهای شخصی
- ارسال خودکار نتیجه به Telegram
- تاریخچهٔ ساخت‌ها و امکان تکرار تنظیمات قبلی

### ریسک‌ها و ملاحظات

- endpoint نباید API key پنل را به frontend بدهد.
- فقط حساب‌های متعلق به همان portal principal قابل انتخاب‌اند.
- ساخت گروهی باید با capacity و panel limits سازگار باشد.
- شکست وسط batch نباید نتیجهٔ موفق‌ها را پنهان کند یا دوباره آن‌ها را بسازد.
- عملیات باید operation ID و idempotency داشته باشد.

### معیارهای پذیرش

- [ ] تمام حالت‌های ساخت فعلی ربات در پورتال موجود باشند.
- [ ] tenant دیگر قابل انتخاب یا دسترسی نباشد.
- [ ] double-click کاربر تکراری ایجاد نکند.
- [ ] خروجی با وضعیت واقعی پنل برابر باشد.
- [ ] خطای جزئی batch واضح نمایش داده شود.
- [ ] تست authorization، capacity، partial failure و idempotency اضافه شود.

### مشخصات تصمیم

- اندازه: M
- ریسک: متوسط
- Migration: احتمالاً ندارد؛ همراه Audit Log ممکن است داشته باشد
- وابستگی پیشنهادی: ۱ و ۱۷
- وضعیت: REJECTED
- تصمیم مالک محصول (2026-07-16): ساخت کاربر همین حالا از ربات نماینده و پنل اصلی
  Hiddify ممکن است؛ افزودن سطح سوم در پورتال ارزش کافی در برابر هزینهٔ نگهداری ندارد.

---

## ۳. مرکز کامل مدیریت فروشگاه در پورتال

### مسئلهٔ فعلی

مدیریت storefront تقریباً کامل داخل ربات فروشگاهی انجام می‌شود. منوی ادمین فروشگاه
۱۴ گزینه دارد و بسیاری از عملیات‌ها شامل واردکردن متن، انتخاب callback و دریافت پیام
جدید هستند. portal نماینده هیچ route مربوط به storefront ندارد.

شواهد فعلی:

- backend/app/bot/storefront/keyboards.py:23
- backend/app/bot/storefront/handlers.py
- frontend/src/portal/PortalApp.tsx:42
- backend/app/services/storefront.py

### خروجی نهایی مورد انتظار

بخش مستقل «فروشگاه من» در portal با navigation و صفحات اختصاصی.

### زیرصفحه‌های پیشنهادی

#### داشبورد فروشگاه

- فروش امروز و ماه
- تعداد خرید و تمدید
- مشتری کل و فعال ۳۰روزه
- سرویس فعال و نزدیک انقضا
- شارژ در انتظار
- تعهد کیف پول
- مصرف کدهای هدیه
- خطاهای provisioning
- conversion تست رایگان به خرید

#### مدیریت پلن‌ها

- ایجاد، ویرایش، حذف و فعال/غیرفعال
- مرتب‌سازی drag-and-drop
- preview ظاهر مشتری
- نمایش قیمت و حاشیه سود
- تاریخچهٔ تغییر قیمت

#### مدیریت مشتریان

- search، filter و pagination
- کارت ۳۶۰درجهٔ مشتری
- کیف پول و ledger
- سرویس‌ها و مصرف
- شارژ یا کسر دستی
- ban/unban
- پیام مستقیم
- lifetime value

#### پرداخت و شارژ

- مشاهده proof و TXID
- تأیید، رد یا اصلاح مبلغ
- عملیات گروهی
- فیلتر روش، مبلغ، تاریخ و وضعیت
- نمایش bonus ناشی از code

#### تنظیمات

- روش‌های پرداخت
- تست رایگان
- عضویت اجباری
- پیام خوش‌آمد
- وضعیت باز/بسته
- مدیران
- پشتیبانی
- preview نمای مشتری

#### کدهای شارژ و هدیه

- CRUD کامل
- بازهٔ اعتبار
- محدودیت کل و هر مشتری
- حداقل top-up
- گزارش conversion و هزینه

### تقسیم نهایی Release

- مرحله A / plan 002: shell، انتخاب فروشگاه و dashboard/read-only
- مرحله B / plan 003: shared command، audit/idempotency، پلن‌ها و تنظیمات
- مرحله C / plan 004: مشتریان، سرویس‌ها و عملیات Hiddify
- مرحله D / plan 005: کیف پول، ledger، رسیدها و تصمیم شارژ
- مرحله E / plan 006: کدهای شارژ و ارسال پیام durable
- مرحله F / plan 007: parity نهایی، deep linkها و خلوت‌سازی منوی ربات

### ریسک‌ها و ملاحظات

- این پروژه نباید در یک Release بزرگ انجام شود.
- bot و portal نباید business logic جداگانه و واگرا داشته باشند.
- تمام mutationها باید tenant-scoped، idempotent و audit‌شده باشند.
- co-admin permission باید قبل از گسترش عملیات حساس مشخص شود.
- wallet و provisioning نیازمند تست PostgreSQL concurrency هستند.

### معیارهای پذیرش

- [x] هر قابلیت روزمرهٔ admin bot معادل وب داشته باشد.
- [x] ربات پس از انتقال، اعلان و shortcut باقی بماند.
- [x] service layer مشترک باشد.
- [x] هیچ mutation فقط با check سمت frontend محافظت نشود.
- [x] wallet و panel action دقیقاً یک‌بار اجرا شوند.
- [x] هر مرحله جداگانه قابل Release و rollback باشد.

### مشخصات تصمیم

- اندازه: XL
- ریسک: متوسط تا زیاد
- Migration: بله
- وابستگی: ۶، ۱۲، ۱۴، ۱۵ و ۱۷ با این پروژه مرتبط‌اند
- وضعیت: DONE — همهٔ شش Release (A تا F) منتشر و روی تولید مستقر شدند: A `v1.83.0/.1`،
  B `v1.84.0` (plan 003)، C `v1.85.0` (plan 004)، D `v1.86.0` (plan 005)، E `v1.87.0` (plan 006)،
  F `v1.88.0` (plan 007 — پَریتی ربات/پورتال، deep-link امن، خانهٔ فشردهٔ مالک). مرزهای باقی‌مانده:
  دسترسی وب هم‌ادمین‌ها (مورد ۱۵)، CRM (مورد ۱۴)، حسابداری (مورد ۱۲).
- برنامه‌های اجرایی: `plans/002-storefront-portal-foundation.md` تا
  `plans/007-storefront-parity-and-bot-simplification.md`
- مبنای اجرایی Release A: commit `b37f587`؛ اصلاح metering نسخهٔ `v1.82.4` پیش از شروع
  در `main` ثبت شده و خارج از scope این قابلیت است.

---

## ۴. فاکتور و وصول مطالبات نماینده از زیرمجموعه‌ها

### مسئلهٔ فعلی

نماینده می‌تواند گزارش و PDF فروش زیرمجموعه را بگیرد، ولی workflow کامل مالی برای
فاکتورکردن زیرمجموعه ندارد. PDF به‌تنهایی invoice state، پرداخت، سررسید، یادآوری و
تاریخچهٔ مالی ایجاد نمی‌کند.

شواهد فعلی:

- backend/app/api/portal.py:735
- backend/app/services/invoice_pdf.py
- backend/app/services/reseller_report.py

### خروجی نهایی مورد انتظار

سامانه چندسطحی شود: owner به reseller فاکتور بدهد و reseller نیز بتواند با قوانین
قیمت‌گذاری خودش به sub-reseller فاکتور صادر کند.

### قابلیت‌های MVP

- draft و preview فاکتور زیرمجموعه
- قیمت اختصاصی هر زیرمجموعه
- صدور و ارسال
- وضعیت sent/overdue/paid/canceled
- سررسید و مهلت پرداخت
- ثبت proof یا TXID
- تأیید یا رد توسط بالادستی
- تاریخچهٔ مالی مستقل
- PDF با issuer صحیح
- گزارش بدهکاران زیرمجموعه

### نسخه‌های بعدی

- صدور خودکار ماهانه
- minimum sale و subscription fee
- یادآوری و dunning
- enforcement اختیاری
- تسویهٔ چند فاکتور
- dispute و اصلاحیه
- برند و شماره‌گذاری اختصاصی

### تصمیم معماری کلیدی

فاکتور owner→reseller نباید با reseller→sub-reseller اشتباه شود. گزینه‌های درست:

- مدل جداگانهٔ ResellerInvoice
- یا مدل invoice عمومی با issuer_type، issuer_id، recipient_type و recipient_id

هر راه باید ledger و گزارش بدهی هر سطح را کاملاً مستقل نگه دارد.

### ریسک‌ها و ملاحظات

- این یک money path جدید و پرریسک است.
- حذف reseller نباید تاریخچهٔ مالی را نابود کند.
- پرداخت یک سطح نباید invoice سطح دیگر را paid کند.
- authorization باید subtree و issuer را هم‌زمان بررسی کند.
- قیمت‌گذاری reseller مستقل از owner pricing است.

### معیارهای پذیرش

- [ ] issuer و recipient هر فاکتور صریح باشند.
- [ ] owner debt و sub-reseller debt مخلوط نشوند.
- [ ] پرداخت فقط فاکتورهای همان رابطه را settle کند.
- [ ] تاریخچه پس از حذف یا تغییر ساختار باقی بماند.
- [ ] concurrency تأیید/رد و پرداخت پوشش داده شود.
- [ ] PDF و UI نام issuer درست را نشان دهند.

### مشخصات تصمیم

- اندازه: XL
- ریسک: زیاد
- Migration: حتماً
- وابستگی: ۶، ۱۵، ۱۷ و ۱۸
- وضعیت: CANDIDATE

---

## ۵. بازطراحی کامل منوی ربات‌ها

### مسئلهٔ فعلی

منوی نماینده ۱۱ گزینه و منوی admin فروشگاه ۱۴ گزینه دارد. reply keyboard دائمی
فضای زیادی از موبایل می‌گیرد و قابلیت‌های روزمره و نادر را هم‌سطح نمایش می‌دهد.

شواهد فعلی:

- backend/app/bot/keyboards.py:84
- backend/app/bot/storefront/keyboards.py:23

### خروجی نهایی مورد انتظار

ربات به gateway سبک تبدیل شود و مدیریت سنگین به portal منتقل شود.

### منوی پیشنهادی نماینده

- 🌐 پورتال مدیریت
- ➕ ساخت سرویس
- 🧾 فاکتور و پرداخت
- 💬 پشتیبانی
- ⋯ بیشتر

### منوی پیشنهادی admin فروشگاه

- 🌐 مدیریت فروشگاه
- 🧾 شارژهای در انتظار
- 👥 مشتریان
- 💬 پیام‌ها
- ⋯ بیشتر

### رفتارهای لازم

- menu بر اساس role و feature flag ساخته شود.
- گزینهٔ غیرمجاز اصلاً نشان داده نشود.
- اعلان‌ها deep-link به صفحهٔ مربوط داشته باشند.
- عملیات پیچیده دکمهٔ «ادامه در پورتال» داشته باشد.
- /start و /menu منوی صحیح را بازسازی کنند.
- fallback برای کاربر قدیمی حفظ شود.
- universal cancel فعلی از بین نرود.

### ترتیب صحیح اجرا

قابلیت‌ها ابتدا باید در portal آماده شوند؛ سپس دکمه‌های اضافی bot حذف یا به «بیشتر»
منتقل شوند. این مورد به‌تنهایی نباید دسترسی کاربران به قابلیت موجود را قطع کند.

### معیارهای پذیرش

- [ ] menu اصلی حداکثر پنج گزینه داشته باشد.
- [ ] عملیات پرتکرار حداکثر در دو لمس قابل دسترسی باشد.
- [ ] هیچ feature بدون جایگزین حذف نشود.
- [ ] تمام role combinations تست شوند.
- [ ] ورود به portal مستقیم باشد.
- [ ] FSMهای فعال با زدن menu به‌درستی cancel شوند.

### مشخصات تصمیم

- اندازه: M
- ریسک: متوسط
- Migration: ندارد
- وابستگی پیشنهادی: بعد از ۱، ۲ و حداقل مرحلهٔ اول ۳
- وضعیت: DONE (v1.89.0) — هر دو ربات (اصلی و فروشگاهی) به منوی reply-keyboard دِک‌شده با
  ≤۵ گزینهٔ سطح‌بالا + «⋯ بیشتر» منتقل شدند؛ کاربرِ بدون پنل «🔗 ثبت پنل» را در همان ابتدا می‌بیند؛
  دکمهٔ یک‌کلیکیِ پورتال (inline) کنار منو؛ و همهٔ عملیات چندمرحله‌ای «قفل» شدند (فقط «✖️ انصراف»
  یا `/start` خروج می‌دهد). بدون migration.

---

## ۶. سیستم پایدار تیکت، درخواست و مکالمه

### مسئلهٔ فعلی

پیام support و درخواست افزایش ظرفیت فقط به Telegram owner ارسال می‌شوند و رکورد
پایداری ندارند. restart، حذف پیام یا شلوغی Telegram می‌تواند context رسیدگی را از بین
ببرد. notificationهای portal نیز feed مشتق‌شده‌اند.

شواهد فعلی:

- backend/app/api/portal.py:707
- backend/app/api/portal.py:868
- backend/app/api/portal.py:904

### خروجی نهایی مورد انتظار

موتور عمومی WorkflowRequest/Ticket برای درخواست‌های مختلف.

### انواع اولیه

- support
- capacity increase
- invoice dispute
- payment review
- panel problem
- account change
- storefront incident

### داده‌های هر درخواست

- tenant و requester
- type و subject
- status و priority
- assignee
- messages
- attachments
- created/updated/resolved timestamps
- SLA
- related invoice/payment/panel/reseller
- resolution
- audit trail

### وضعیت‌ها

new → acknowledged → in_progress → waiting_user → resolved → closed

### UX نماینده

- ثبت درخواست
- مشاهده status
- پاسخ و ارسال فایل
- تاریخچه
- اعلان Telegram
- reopen در صورت نیاز

### UX مالک

- queue و filter
- تعیین مسئول
- پاسخ آماده
- تغییر priority
- merge درخواست تکراری
- resolve/reject
- گزارش زمان پاسخ

### ریسک‌ها و ملاحظات

- attachment validation و access control لازم است.
- owner notification باید mirror رکورد DB باشد، نه منبع اصلی.
- reply تلگرام باید به thread صحیح متصل شود.
- SLA و timestamp باید timezone-safe باشند.

### معیارهای پذیرش

- [ ] هیچ درخواست فقط در Telegram ذخیره نشود.
- [ ] restart باعث گم‌شدن state نشود.
- [ ] tenant isolation برای ticket و attachment تست شود.
- [ ] reply از portal و Telegram یک timeline بسازند.
- [ ] owner بتواند unresolved و overdue را ببیند.
- [ ] notification failure خود ticket را از بین نبرد.

### مشخصات تصمیم

- اندازه: L
- ریسک: متوسط
- Migration: حتماً
- وابستگی: پایهٔ پیشنهاد ۷
- وضعیت: CANDIDATE

---

## ۷. صندوق عملیات یکپارچه برای مالک

### مسئلهٔ فعلی

آیتم‌های نیازمند اقدام بین Telegram، Payments، Logs، Panels و عملیات storefront
پخش‌اند. owner باید خودش تشخیص دهد چه چیزی عقب مانده است.

### خروجی نهایی مورد انتظار

صفحهٔ «مرکز عملیات» با دسته‌های زیر:

- بحرانی
- نیازمند اقدام امروز
- در انتظار پاسخ کاربر
- ناموفق و نیازمند retry
- قدیمی‌تر از SLA
- بدون مسئول
- recovery pending

### منابع داده

- Payment
- WorkflowRequest
- SyncRun
- EnforcementAction
- StorefrontOperation
- StorefrontWalletTxn
- bot/storefront error state

### عملیات مستقیم

- confirm/reject payment
- approve/reject capacity
- پاسخ ticket
- retry sync
- retry panel action
- بازکردن context کامل
- assign و snooze
- bulk resolve موارد کم‌خطر

### طراحی معماری

این صفحه نباید رکوردهای اصلی را duplicate کند. یک query/read-model تجمیعی با لینک به
source entity مناسب‌تر است. فقط assignment، snooze و acknowledgement ممکن است state
جداگانه بخواهند.

### معیارهای پذیرش

- [ ] تمام کارهای unresolved در یک صفحه دیده شوند.
- [ ] هر item لینک مستقیم به context داشته باشد.
- [ ] انجام action بلافاصله queue را به‌روزرسانی کند.
- [ ] duplicate item برای یک source ساخته نشود.
- [ ] filter، sort، severity و SLA وجود داشته باشد.
- [ ] permission owner/operator enforce شود.

### مشخصات تصمیم

- اندازه: M/L
- ریسک: متوسط
- Migration: احتمالاً از پیشنهاد ۶
- وابستگی: ۶ و ترجیحاً ۱۷
- وضعیت: CANDIDATE

---

## ۸. درخت کامل زیرمجموعه‌ها و عملیات گروهی

### مسئلهٔ فعلی

portal فقط direct childهای هر root را فهرست می‌کند؛ درحالی‌که backend descendantهای
کامل را برای authorization می‌شناسد. شبکهٔ چندسطحی در UI تخت و ناقص دیده می‌شود.

شواهد فعلی:

- backend/app/api/portal.py:348
- backend/app/api/portal.py:391

### خروجی نهایی مورد انتظار

نمای درختی با parent، depth، breadcrumb و summary هر node.

### قابلیت‌ها

- expand/collapse
- search کل subtree
- breadcrumb
- فروش node و subtree
- ظرفیت و user count
- enforcement state
- GB cap
- debt و invoice summary
- filter بر اساس panel/status
- export شاخه

### عملیات گروهی

- تعیین cap
- freeze
- restore
- پیام
- PDF
- افزایش ظرفیت

هر عملیات گروهی باید preview تعداد reseller و user affected را نشان دهد و سپس با
operation ID اجرا شود.

### ملاحظات performance

- API باید tree را با queryهای bounded بسازد.
- node_report نباید برای صدها node به N+1 بزرگ تبدیل شود.
- pagination یا lazy-load شاخه‌ها برای دادهٔ بزرگ لازم است.

### معیارهای پذیرش

- [ ] تمام descendantها در جای درست نمایش داده شوند.
- [ ] دسترسی بیرون subtree ممکن نباشد.
- [ ] tree بزرگ query explosion ایجاد نکند.
- [ ] bulk action preview و audit داشته باشد.
- [ ] partial failure نتیجهٔ هر node را مشخص کند.
- [ ] mobile UX قابل استفاده باشد.

### مشخصات تصمیم

- اندازه: M/L
- ریسک: متوسط
- Migration: احتمالاً ندارد
- وابستگی پیشنهادی: ۱۷
- وضعیت: CANDIDATE

---

## ۹. Mini App یا پورتال مشتریان فروشگاه

### مسئلهٔ فعلی

customer storefront فقط از طریق bot خرید، wallet، service و support را مدیریت می‌کند.
برای مشاهدهٔ چند سرویس، مصرف، QR و تاریخچهٔ مالی، chat interface محدود است.

شواهد فعلی:

- backend/app/bot/storefront/keyboards.py:39
- backend/app/bot/storefront/handlers.py:223

### خروجی نهایی مورد انتظار

Mini App چندمستاجری با برند هر فروشگاه.

### صفحات پیشنهادی

#### خانه

- موجودی
- سرویس فعال
- نزدیک انقضا
- notification
- پیشنهاد خرید

#### سرویس‌ها

- مصرف زنده
- quota و روز باقی‌مانده
- QR و subscription link
- copy و open-in-client
- تمدید
- enable/disable
- delete
- auto-renew

#### کیف پول

- top-up
- transaction history
- proof status
- gift/credit code
- refund/reversal

#### فروشگاه

- plan list
- خرید
- trial
- قوانین
- support

### امنیت

- tenant از bot context معتبر استخراج شود.
- Telegram init/auth data در backend validate شود.
- customer ID ورودی قابل اعتماد نباشد.
- subscription link و proof حساس محسوب شوند.
- purchase/renew از operation model امن فعلی استفاده کند.

### معیارهای پذیرش

- [ ] چهار قابلیت اصلی customer bot در Mini App موجود باشند.
- [ ] tenant isolation کامل باشد.
- [ ] refresh و double-click پول یا سرویس را تکرار نکند.
- [ ] مصرف live و link به owner صحیح محدود باشد.
- [ ] app در موبایل و Telegram webview درست کار کند.
- [ ] bot fallback باقی بماند.

### مشخصات تصمیم

- اندازه: XL
- ریسک: زیاد
- Migration: احتمالاً بله
- وابستگی: ۱، ۳، ۱۰ و ۲۰
- وضعیت: CANDIDATE

---

## ۱۰. تمدید خودکار سرویس از کیف پول

### مسئلهٔ فعلی

wallet، ledger و renewal امن وجود دارند، ولی customer باید دستی تمدید کند.

شواهد فعلی:

- backend/app/services/storefront_subscription.py:72
- backend/app/models/storefront.py:197

### خروجی نهایی مورد انتظار

Auto-renew opt-in برای هر service.

### تنظیمات مشتری

- enabled
- renewal plan
- چند روز پیش از انقضا
- max acceptable price
- notify before debit
- قبول/رد تغییر قیمت

### جریان

۱. scheduler سرویس نزدیک انقضا را پیدا کند.  
۲. status، plan و consent را دوباره بررسی کند.  
۳. wallet را atomically debit کند.  
۴. target renewal را ذخیره و اعمال کند.  
۵. نتیجه را finalize کند.  
۶. در failure از recovery فعلی استفاده کند.  
۷. notification بفرستد.

### حالت‌های مرزی

- موجودی ناکافی
- plan حذف یا disabled
- price increase
- panel unavailable
- renewal هم‌زمان دستی
- خاموش‌کردن auto-renew هنگام job
- workerهای هم‌زمان
- crash پس از debit

### معیارهای پذیرش

- [ ] هر cycle حداکثر یک debit و renewal داشته باشد.
- [ ] price بالاتر از consent اعمال نشود.
- [ ] failure مبهم free renewal یا lost money نسازد.
- [ ] retry bounded و idempotent باشد.
- [ ] customer بتواند history و دلیل failure را ببیند.
- [ ] low-balance notification ایجاد شود.

### مشخصات تصمیم

- اندازه: M/L
- ریسک: زیاد
- Migration: حتماً
- وابستگی پیشنهادی: ۹ و ۱۷
- وضعیت: CANDIDATE

---

## ۱۱. تطبیق و تأیید خودکار پرداخت رمزارزی

### مسئلهٔ فعلی

سامانه deposit را بررسی می‌کند، ولی decision نهایی owner دستی است. top-up فروشگاه نیز
پس از proof در انتظار admin می‌ماند.

شواهد فعلی:

- backend/app/services/payments.py:1
- backend/app/api/payments.py:130
- backend/app/bot/storefront/handlers.py:1473

### خروجی نهایی مورد انتظار

auto-confirm اختیاری و policy-based برای on-chain payment قطعی.

### شروط auto-confirm

- network صحیح
- token contract صحیح
- destination صحیح
- amount کافی
- confirmations کافی
- unique txid
- invoice هنوز payable
- amount زیر threshold
- نرخ و tolerance معتبر

### modeها

- off
- suggestion only
- auto-confirm مبالغ کوچک
- auto-confirm کامل شبکه‌های منتخب

### موارد دستی

- card screenshot
- مبلغ ناکافی
- token یا destination اشتباه
- confirmation ناکافی
- RPC ambiguity
- اختلاف نرخ زیاد

### مدل و job

- PaymentVerificationAttempt
- pending/verified/failed/ambiguous
- scheduler recheck
- snapshot پاسخ chain
- policy version
- finalization با service فعلی

### معیارهای پذیرش

- [ ] TXID دوبار مصرف نشود.
- [ ] پرداخت ناقص auto-confirm نشود.
- [ ] RPC failure تصمیم قطعی نسازد.
- [ ] owner همهٔ تصمیم‌های خودکار را audit کند.
- [ ] policy خاموش پیش‌فرض باشد.
- [ ] storefront و owner payment tenant-safe باشند.

### مشخصات تصمیم

- اندازه: L
- ریسک: زیاد
- Migration: حتماً
- وابستگی: ۱۷
- وضعیت: CANDIDATE

---

## ۱۲. داشبورد سود و زیان واقعی فروشگاه

### مسئلهٔ فعلی

فروش، top-up، customer، service و wallet liability گزارش می‌شوند، ولی سود واقعی و
cost of service وجود ندارد.

شواهد فعلی:

- backend/app/services/storefront.py:440

### خروجی نهایی مورد انتظار

- gross sales
- refund و reversal
- net sales
- service cost
- gross profit
- margin
- top-up cash received
- wallet liability
- reseller debt to owner
- cash flow
- promotion cost
- profit by plan/customer

### قانون حسابداری

top-up درآمد نیست؛ افزایش تعهد wallet است. revenue هنگام purchase/renewal شناسایی
می‌شود. refund و reversal نیز باید دوره و علت صحیح داشته باشند.

### فیلترها

- روز/هفته/ماه/بازه
- storefront
- plan
- customer
- panel
- transaction kind

### نیاز داده‌ای

در صورت تغییر owner pricing، سود گذشته نباید بازنویسی شود. احتمالاً cost snapshot
هنگام purchase/renewal لازم است.

### معیارهای پذیرش

- [ ] گزارش با wallet ledger reconciliation شود.
- [ ] top-up revenue حساب نشود.
- [ ] refund/reversal درست کم شوند.
- [ ] تعریف cost و revenue مستند باشد.
- [ ] تغییر تعرفه سود گذشته را عوض نکند.
- [ ] export با عدد dashboard برابر باشد.

### مشخصات تصمیم

- اندازه: M/L
- ریسک: متوسط
- Migration: شاید برای cost snapshot
- وابستگی: پایهٔ ۱۳ و ۱۹
- وضعیت: CANDIDATE

---

## ۱۳. محافظ قیمت و حاشیه سود پلن‌ها

### مسئلهٔ فعلی

StorefrontPlan قیمت ثابت دارد، ولی هنگام تعریف plan هزینهٔ احتمالی owner و margin را
نشان نمی‌دهد.

شواهد فعلی:

- backend/app/models/storefront.py:102

### خروجی نهایی مورد انتظار

در create/edit plan:

- estimated cost
- profit
- margin percent
- suggested price
- loss warning
- configurable minimum margin

### policyها

- warning only
- require confirmation
- hard floor
- promotion override
- trial exemption
- customer-specific override

### تصمیم محاسباتی

cost estimate باید بر اساس pricing واقعی owner تعریف شود. cost snapshot هنگام sale
ذخیره شود تا تغییر تعرفه history را بازنویسی نکند.

### معیارهای پذیرش

- [ ] plan زیان‌ده بدون warning ایجاد نشود.
- [ ] hard floor در stale client/API نیز enforce شود.
- [ ] override دلیل و actor داشته باشد.
- [ ] promotion از rule عادی تفکیک شود.
- [ ] history با تغییر pricing ثابت بماند.
- [ ] margin dashboard با plan editor سازگار باشد.

### مشخصات تصمیم

- اندازه: M
- ریسک: متوسط
- Migration: احتمالاً cost snapshot
- وابستگی: ۱۲
- وضعیت: CANDIDATE

---

## ۱۴. CRM و اتوماسیون بازاریابی فروشگاه

### مسئلهٔ فعلی

segmentهای مفید موجودند، اما broadcast دستی و status آن موقت است.

شواهد فعلی:

- backend/app/bot/storefront/keyboards.py:329
- backend/app/services/broadcast.py:356

### خروجی نهایی مورد انتظار

Campaign پایدار با:

- audience
- template
- schedule
- status
- recipient snapshot
- sent/blocked/failed
- conversion
- credit code
- recurrence

### triggerها

- trial ended/no purchase
- expiring soon
- expired
- inactive 30d
- wallet positive/no service
- usage high
- first purchase
- loyal customer

### محافظت‌ها

- audience preview
- max recipients
- quiet hours
- cancel
- dedup
- opt-out
- flood control
- tenant isolation

### معیارهای پذیرش

- [ ] restart campaign state را از بین نبرد.
- [ ] recipient تکراری پیام تکراری نگیرد.
- [ ] conversion campaign اندازه‌گیری شود.
- [ ] cancel واقعاً ارسال آینده را متوقف کند.
- [ ] preview و actual audience اختلاف گزارش‌شده داشته باشند.
- [ ] owner/admin permission رعایت شود.

### مشخصات تصمیم

- اندازه: L
- ریسک: متوسط
- Migration: حتماً
- وابستگی: ۳، ۶ و ۱۷
- وضعیت: CANDIDATE

---

## ۱۵. نقش‌ها و دسترسی تیمی

### مسئلهٔ فعلی

co-admin فروشگاه Telegram ID است و نقش ریزدانه ندارد. portal نیز به هویت Telegram
نماینده متکی است.

شواهد فعلی:

- backend/app/models/storefront.py:87
- backend/app/bot/storefront/handlers.py:128

### خروجی نهایی مورد انتظار

Roleهای پایه:

- owner
- full admin
- finance
- sales
- support
- customer operator
- viewer
- campaign manager

### permissionها

- view analytics
- view finance
- approve topup
- edit plans
- edit payment settings
- adjust wallet
- broadcast
- ban customer
- manage admins
- view subscription links

### دعوت و session

- Telegram invite
- one-time invite link
- role assignment
- expiry
- revoke
- 2FA برای finance
- session epoch

### اصول امنیتی

- backend permission enforcement
- tenant-scoped role
- permanent owner
- immediate revoke
- audit role change
- secret field masking

### معیارهای پذیرش

- [ ] support role نتواند wallet تغییر دهد.
- [ ] finance role نتواند owner را حذف کند.
- [ ] revoke فوری sessionها را ببندد.
- [ ] هر mutation actor مشخص داشته باشد.
- [ ] UI فقط actionهای مجاز را نمایش دهد.
- [ ] API مستقیم action غیرمجاز را رد کند.

### مشخصات تصمیم

- اندازه: L
- ریسک: زیاد
- Migration: حتماً
- وابستگی: ۱۷؛ بهتر است پیش از گسترش کامل ۳
- وضعیت: CANDIDATE

---

## ۱۶. پیش‌نمایش دقیق enforcement

### مسئلهٔ فعلی

dry-run وجود دارد ولی owner صفحه‌ای ندارد که پیش از فعال‌سازی دقیقاً affected reseller،
user، limit و علت را ببیند.

شواهد فعلی:

- docs/NEXTBATCH_PLAN.md:507
- backend/app/services/enforcement.py
- backend/app/services/dunning.py

### خروجی نهایی مورد انتظار

گزارش «اگر اکنون اجرا شود چه اتفاقی می‌افتد؟»

### اطلاعات

- reseller
- invoice/debt
- overdue days
- deferred date
- panel
- affected users
- planned action
- API readiness
- sync freshness
- stale warning
- skip reason

### عملیات

- dry-run
- select/deselect
- final confirmation
- export
- execute batch
- progress
- retry
- restore

### اصل صحت

preview مجوز اجرای کور نیست. execution باید state را دوباره بررسی کند؛ زیرا پرداخت یا
defer ممکن است بین preview و اجرا تغییر کند.

### معیارهای پذیرش

- [ ] preview هیچ external write نداشته باشد.
- [ ] affected count دقیق باشد.
- [ ] execution revalidation داشته باشد.
- [ ] preview/execution drift گزارش شود.
- [ ] stale sync واضح باشد.
- [ ] owner بتواند موارد خاص را exclude کند.

### مشخصات تصمیم

- اندازه: M
- ریسک: متوسط
- Migration: احتمالاً ندارد
- وابستگی پیشنهادی: ۱۷
- وضعیت: CANDIDATE

---

## ۱۷. Audit Log جامع و تغییرناپذیر

### مسئلهٔ فعلی

DeliveryLog، EnforcementAction و SyncRun وجود دارند، ولی mutationهای عمومی owner،
reseller، storefront admin و system در یک audit trail مشترک ثبت نمی‌شوند.

شواهد فعلی:

- backend/app/models/logs.py:30

### خروجی نهایی مورد انتظار

AuditEvent append-only:

- timestamp
- actor type/id
- tenant
- source
- action
- entity type/id
- before/after
- request/correlation ID
- result
- metadata redacted

### actionهای مهم

- settings
- roles
- payment decision
- wallet adjustment
- plan changes
- suspension
- service create/delete
- price change
- restore
- payment settings
- campaign
- sensitive export
- backup download/restore

### حفاظت داده

- secretها در before/after ذخیره نشوند.
- password/token/API key redact شوند.
- append-only policy وجود داشته باشد.
- retention جدا تعریف شود.
- actor system و human تفکیک شوند.

### UI

- filter actor/action/entity/date
- before/after diff
- entity link
- timeline در صفحه entity
- export

### معیارهای پذیرش

- [ ] تمام mutationهای منتخب event بسازند.
- [ ] secret در audit نباشد.
- [ ] علت تغییر wallet/status قابل بازسازی باشد.
- [ ] actor و source دقیق باشند.
- [ ] audit failure سیاست مشخص داشته باشد.
- [ ] retention و access permission تست شوند.

### مشخصات تصمیم

- اندازه: M/L
- ریسک: متوسط
- Migration: حتماً
- وابستگی: بهتر است پیش از ۳، ۴، ۱۰، ۱۱ و ۱۵
- وضعیت: CANDIDATE

---

## ۱۸. خروجی حسابداری و مرکز تسویه

### مسئلهٔ فعلی

owner برخی CSVها دارد، اما portal reseller و storefront export و reconciliation مالی
جامع ندارند.

شواهد فعلی:

- frontend/src/csv.ts
- frontend/src/portal/PortalApp.tsx

### خروجی‌های نماینده

- sales
- invoices
- payments
- subs
- cost/profit
- debts

### خروجی‌های فروشگاه

- wallet ledger
- topups
- purchases/renewals
- refunds
- credit codes
- customers
- services
- profit by plan

### خروجی‌های owner

- reseller settlements
- debt aging
- accounting summary
- payment reconciliation
- storefront monthly fees
- collection status

### فرمت و معماری

- CSV با encoding سازگار Excel
- XLSX چندsheet در صورت نیاز
- PDF summary
- async export برای دادهٔ بزرگ
- expiring download
- webhook/API در مرحله بعد

### حفاظت

- permission مالی
- audit export
- date/size limits
- temporary files
- عدم خروج secret/subscription link مگر صریح

### معیارهای پذیرش

- [ ] export با UI/ledger برابر باشد.
- [ ] فارسی در Excel صحیح باشد.
- [ ] export بزرگ timeout نکند.
- [ ] permission و audit وجود داشته باشد.
- [ ] فایل موقت expire شود.
- [ ] reconciliation discrepancy گزارش شود.

### مشخصات تصمیم

- اندازه: M
- ریسک: کم تا متوسط
- Migration: معمولاً ندارد
- وابستگی: ۱۲، ۱۵ و ۱۷
- وضعیت: CANDIDATE

---

## ۱۹. پیش‌بینی پایان ماه و هشدار ناهنجاری

### مسئلهٔ فعلی

daily trend و monthly history وجود دارند، ولی forecast و anomaly detection ارائه
نمی‌شود.

شواهد فعلی:

- backend/app/api/portal.py:125
- backend/app/api/portal.py:184

### خروجی نهایی مورد انتظار

- forecast invoice end-of-month
- storefront sales forecast
- capacity exhaustion date
- growth/decline
- churn-risk customers
- abnormal sub usage
- purchase/renewal anomaly
- wallet liability spike
- trial conversion drop

### روش نسخه اول

- moving average
- same-day previous-month comparison
- seasonal baseline
- confidence range
- threshold anomaly

AI پیچیده برای MVP لازم نیست.

### UX

- برچسب «پیش‌بینی»
- confidence interval
- explanation
- insufficient-data state
- dismiss/snooze alert
- عدم اجرای مالی خودکار

### معیارهای پذیرش

- [ ] data leakage از آینده نباشد.
- [ ] دادهٔ کم قطعیت کاذب نسازد.
- [ ] forecast با actual قابل ارزیابی باشد.
- [ ] anomaly دلیل قابل فهم داشته باشد.
- [ ] خطای مدل business action خودکار اجرا نکند.
- [ ] metrics evaluation ذخیره یا گزارش شوند.

### مشخصات تصمیم

- اندازه: M/L
- ریسک: متوسط
- Migration: شاید برای forecast snapshot
- وابستگی: ۱۲
- وضعیت: CANDIDATE

---

## ۲۰. White-label کامل پورتال و فروشگاه

### مسئلهٔ فعلی

welcome text و payment information قابل تنظیم‌اند، ولی portal و storefront هویت بصری
کامل tenant-specific ندارند.

شواهد فعلی:

- backend/app/models/storefront.py:63
- frontend/src/portal/PortalLayout.tsx:111

### خروجی نهایی مورد انتظار

- brand name
- logo
- primary/secondary colors
- welcome text
- contact
- terms
- channel link
- favicon
- PDF identity
- stamp/signature
- receipt template
- notification template

### سطح MVP

برند بر اساس authenticated tenant روی همان دامنهٔ اصلی نمایش داده شود.

### سطح پیشرفته

- subdomain
- custom domain
- automatic TLS
- domain→tenant mapping
- DNS validation

custom domain باید plan جدا داشته باشد؛ operational risk آن از theme ساده بیشتر است.

### PDF و receipt

receipt PDF موجود در پروژه می‌تواند برای storefront customer نیز استفاده شود. invoice،
receipt و portal باید brand یکسان داشته باشند.

### امنیت و فایل

- logo type/size validation
- safe storage
- tenant isolation
- sanitized CSS/color values
- fallback theme
- جلوگیری از domain takeover

### معیارهای پذیرش

- [ ] brand tenant A روی tenant B ظاهر نشود.
- [ ] upload امن باشد.
- [ ] PDF، portal و Mini App consistent باشند.
- [ ] تغییر branding بدون rebuild اعمال شود.
- [ ] fallback برای tenant بدون تنظیم وجود داشته باشد.
- [ ] custom domain ownership validate شود.

### مشخصات تصمیم

- اندازه: M برای branding؛ L/XL برای custom domain
- ریسک: متوسط
- Migration: بله
- وابستگی: ۳ و ۹
- وضعیت: CANDIDATE

---

## وابستگی‌های کلیدی

- ۱ پیش‌نیاز UX مناسب برای ۵ و ۹ است.
- ۲ باید قبل از حذف گزینهٔ ساخت کاربر از منوی ربات در ۵ آماده شود.
- ۳ بهتر است چندمرحله‌ای اجرا شود و از ۱۵ و ۱۷ برای عملیات حساس بهره ببرد.
- ۶ پایهٔ اصلی ۷ است.
- ۹ می‌تواند از APIهای ایجادشده در ۳، و auto-renew در ۱۰ استفاده کند.
- ۱۲ پایهٔ محاسباتی ۱۳ و بخشی از ۱۹ است.
- ۱۵ باید پیش از واگذاری عملیات مالی گسترده در ۳ یا ۴ اجرا شود.
- ۱۷ بهتر است قبل از money pathهای جدید ۴، ۱۰ و ۱۱ اجرا شود.
- ۲۰ پس از شکل‌گیری صفحات ۳ و ۹ ارزش بیشتری دارد.

## قالب انتخاب توسط مالک پروژه

برای انتخاب، همین فایل یا گفتگو را با قالب زیر به‌روزرسانی کنید:

انتخاب قطعی: ۱، ۲، ۳  
علاقه‌مند ولی بعداً: ۹، ۱۰  
حذف شوند: ۱۱، ۱۴  
نیازمند مقایسه: ۴، ۶

پس از انتخاب:

۱. وابستگی‌ها مشخص می‌شوند.  
۲. MVP از نسخه‌های بعد جدا می‌شود.  
۳. اولویت بر اساس ارزش، effort، risk و dependency تعیین می‌شود.  
۴. برای مورد اول یک plan اجرایی مستقل نوشته می‌شود.  
۵. هر مورد در branch و Release جدا اجرا و تست می‌شود.  
۶. پس از deploy و smoke-check، وضعیت آن به DONE تغییر می‌کند.

## محدودهٔ این سند

این فایل roadmap محصول است، نه audit جدید correctness/security/performance. ایجاد این
فایل هیچ source code، migration، configuration، Release یا production state را تغییر
نمی‌دهد.
