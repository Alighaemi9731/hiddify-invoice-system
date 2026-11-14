# افزونه پنل هیدیفای — نصب مستقل

این افزونه برای قفل رمز و بازگردانی ورود نماینده از طریق سامانه فاکتور است.
**روی هر سرور پنل هیدیفای نصب می‌شود.** نصب اصلی سامانه فاکتور روی سرور مرکزی،
دستور دیگری دارد. هر دو دستور در [README اصلی](../README.md#installation) آمده‌اند.

| دستور | محل اجرا | نتیجه |
|---|---|---|
| `release-installer.sh` | سرور مرکزی سامانه فاکتور | نصب یا به‌روزرسانی سامانه، دیتابیس و بات‌ها |
| `panel-addon-installer.sh` | تک‌تک سرورهای هیدیفای | نصب یا به‌روزرسانی افزونه همان پنل |

## نصب یا به‌روزرسانی افزونه با یک دستور

روی سروری که **هیدیفای قبلاً روی آن نصب و سالم اجرا شده** این خط را اجرا کنید:

```bash
(set -e; d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; cd "$d"; u="https://github.com/Alighaemi9731/hiddify-invoice-system/releases/latest/download"; curl -fsSLO "$u/panel-addon-installer.sh"; curl -fsSLO "$u/panel-addon-installer.sh.sha256"; sha256sum -c panel-addon-installer.sh.sha256; sudo bash panel-addon-installer.sh)
```

ورود دستی اطلاعات پنل، رمز دیتابیس یا لینک مدیریت لازم نیست. نصب‌کننده تنظیمات
محلی را می‌خواند، نسخه فعال را تشخیص می‌دهد، بکاپ می‌گیرد، افزونه مناسب را نصب
می‌کند و پس از راه‌اندازی مجدد سرویس پنل، بارگذاری آن را بررسی می‌کند. نمایش
`"ready": true` یعنی نصب و بررسی موفق بوده است؛ خروج با کد غیرصفر یعنی کار کامل نشده.

نسخه‌های پشتیبانی‌شده دقیقاً **Hiddify Manager / Panel 12.0.0 و 14.2.0** هستند.
همان دستور برای هر دو نسخه است. پیش‌نیازها: دسترسی root یا sudo، systemd،
`python3`، `curl`، `sha256sum` و ابزار `mariadb-dump` یا `mysqldump` برای پنل‌های MySQL.
این ابزارها معمولاً در نصب استاندارد هیدیفای وجود دارند؛ نبودنشان نصب را متوقف می‌کند.

## رفتار نصب‌کننده

- اسکریپت اولیه و آرشیو Release با SHA-256 منتشرشده بررسی می‌شوند؛ فقط فایل‌های
  افزونه استخراج و اجرا می‌شوند. اسکریپت نصب سامانه فاکتور اجرا نمی‌شود.
- فقط `hiddify-panel.service` برای بارگذاری افزونه ری‌استارت می‌شود. پنل مدیریت
  ممکن است مدت کوتاهی در دسترس نباشد؛ سرویس‌های Xray و sing-box ری‌استارت نمی‌شوند.
- اطلاعات ورود محلی و پاسخ‌های دارای اطلاعات حساس چاپ نمی‌شوند؛ آزمون سلامت
  با درخواست خواندنی به خود سرور انجام می‌شود و رمز نماینده‌ای برای آزمون تغییر نمی‌کند.
- پیش از تغییر، دیتابیس و فایل‌های درگیر در `/var/backups/invoice-panel-addon/`
  با دسترسی محدود root ذخیره می‌شوند. شکست بکاپ مانع تغییر پنل می‌شود.
- در صورت شکست نصب یا تأیید بارگذاری، فایل‌ها و تنظیمات قبلی سرویس بازگردانده
  می‌شوند و راه‌اندازی سرویس دوباره تلاش می‌شود. اگر این تلاش هم شکست بخورد،
  خطا و مسیر بکاپ اعلام می‌شود. دیتابیس خودکار بازنویسی نمی‌شود.
- نصب مجدد همان نسخه روی پنل آماده، بدون بکاپ و ری‌استارت اضافی تمام می‌شود.
- هوک‌های قدیمی شناخته‌شده افزونه به هوک مشترک تبدیل می‌شوند. تنظیمات سفارشی
  سرویس حفظ می‌شوند؛ تداخل یا تغییر مستقل فایل‌های افزونه نیاز به بررسی دارد.

این بکاپ جایگزین بکاپ کامل سرور پیش از **ارتقای خود هیدیفای** نیست. ری‌استارت
پنل می‌تواند migrationهای معوق خود هیدیفای را هم اجرا کند؛ نصب را روی پنل سالم
با ارتقای تمام‌شده اجرا کنید.

## بعد از نصب و ارتقای پنل

بررسی محلی، بدون دانلود و بدون تغییر:

```bash
sudo invoice-panel-addon --check
```

خروجی موفق شامل `installed`، `startup_hook` و `ready` با مقدار `true` است.
`--check` هنگام آماده نبودن، کد خروج ۱ می‌دهد.

هوک systemd در هر شروع سرویس، افزونه مناسب نسخه فعال را اعمال می‌کند. پس از نصب
این بسته روی نسخه ۱۲، در ارتقای **۱۲.۰.۰ به ۱۴.۲.۰** هم نسخه مناسب خودکار انتخاب
می‌شود؛ دستور دستی جدا برای جایگزینی افزونه لازم نیست. بعد از ارتقا حتماً
`--check` و preflight سامانه را اجرا کنید و [راهنمای ارتقا](HIDDIFY_UPGRADE.md) را دنبال کنید.

هوک به اینترنت یا دیتابیس وصل نمی‌شود و فقط فایل‌های بسته محلی را بررسی می‌کند.
برای نسخه یا سورس ناشناخته، عمداً شروع سرویس پنل را متوقف می‌کند؛ پشتیبانی
نسخه‌های آینده باید ابتدا بررسی شود. این افزونه ابزار ارتقای خود هیدیفای نیست.

برای دریافت نسخه جدید افزونه، همان دستور نصب بالا را دوباره اجرا کنید. فایل‌های
بسته نصب‌شده در `/usr/local/lib/invoice-panel-addon/releases/` نگهداری می‌شوند.
نسخه افزونه از Release سامانه می‌آید و مستقل از نسخه نصب‌شده سامانه مرکزی است.

برای نصب نسخه مشخص، به‌جای `latest/download` در آدرس بالا از `download/vX.Y.Z`
استفاده کنید و بخش آخر دستور را `sudo RELEASE_TAG=vX.Y.Z bash panel-addon-installer.sh`
بنویسید؛ bootstrap و آرشیو هر دو باید از همان Release باشند.

## حذف افزونه

روی یک نسخه پشتیبانی‌شده و پنل سالم:

```bash
sudo invoice-panel-addon --uninstall
```

این دستور بکاپ می‌گیرد، تغییرات شناخته‌شده افزونه و هوک شروع سرویس را حذف می‌کند،
سرویس پنل را ری‌استارت و حذف را بررسی می‌کند. دستور محلی و بسته‌های کش‌شده برای
نصب مجدد باقی می‌مانند. حذف افزونه قابلیت بازگردانی ورود بدون رمز در سامانه را
از دسترس خارج می‌کند؛ پیش از آن عملیات قفل/بازگردانی را متوقف و وضعیت نماینده‌ها را بررسی کنید.

برای بررسی خطای هوک در نسخه ناشناخته:

```bash
sudo journalctl -u hiddify-panel.service -n 80 --no-pager
```

ابتدا سازگاری نسخه جدید را بررسی کنید یا با بکاپ کامل به نسخه پشتیبانی‌شده
بازگردید. `--uninstall` روی سورس ناشناخته عمداً فایل‌ها را دست‌کاری نمی‌کند.
در بازیابی اضطراری با آگاهی از غیرفعال‌شدن افزونه، فقط هوک مدیریت‌شده آن را بردارید:

```bash
sudo rm /etc/systemd/system/hiddify-panel.service.d/90-invoice-panel-addon.conf
sudo systemctl daemon-reload
sudo systemctl restart hiddify-panel.service
```

این اقدام فقط اجبار هوک را حذف می‌کند و پاک‌سازی سورس ناشناخته یا تأیید سازگاری نیست.

## Implementation and validation

`deploy/panel-addon-installer.sh` downloads one immutable verified archive and extracts
only the controller, two generation-specific helpers and VERSION. `deploy/panel_addon.py`
selects the active virtual environment and validates both Manager and Panel versions.
The v14 helper pins the original API registration source by SHA-256; the v12 helper
validates the exact edit-form insertion point. Unknown/custom source is rejected.

The root wrapper is `/usr/local/sbin/invoice-panel-addon`; the single managed drop-in
is `90-invoice-panel-addon.conf`. It adds an `ExecStartPre` command without resetting
unrelated commands. Known legacy hooks are migrated only when their effective service
semantics can be preserved. `--apply` is an internal boot hook, not a health check.

Regression coverage: both generations, repeated installation/removal, SQL backup,
failure before mutation, restart/verification rollback, legacy hook migration and
unrelated command preservation, source/version rejection, 12→14 boot selection,
loaded-process freshness, settings extraction without importing the panel application,
and checksum/archive validation. Run:

```bash
bash deploy/test-panel-addon.sh
cd backend && .venv/bin/pytest -q tests/test_panel_addon_installer.py
```
