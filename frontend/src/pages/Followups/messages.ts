import type { CrmSegment } from "../../api/client";

/**
 * One ready-to-send Telegram message per segment — the thing the owner actually does after
 * filtering the board is open ~30 private chats and type the same paragraph 30 times.
 *
 * Rules these texts follow, so an edit does not quietly break them:
 *
 * 1. **Plain text only.** It is copied into Telegram by hand, so no HTML/Markdown markup —
 *    a stray `*` or `_` would render as formatting in one client and as a literal in another.
 * 2. **No numbers that live in settings.** The free-test threshold, the price per GB, the
 *    dunning days and the enabled payment methods are all owner-editable; quoting one here
 *    would turn a settings change into a lie in a message nobody re-reads. The bot already
 *    carries the exact figures per reseller — these texts point at it instead.
 * 3. **True for the WHOLE bucket.** The board copies one text for every reseller in the
 *    segment, so nothing may assume an amount, a date, or a specific panel.
 * 4. The greeting is NOT part of the body: `segmentMessage()` prepends a personalized one
 *    when a single reseller's name is known (the drawer) and a neutral one for bulk use.
 */

const GREETING_BULK = "سلام، وقت بخیر 🌹";
const greetingFor = (name: string) => `${name} عزیز، سلام و وقت بخیر 🌹`;

export const SEGMENT_MESSAGES: Record<CrmSegment, string> = {
  suspended: `پنل نمایندگی شما در حال حاضر به دلیل تسویه‌نشدن فاکتور سررسیدشده مسدود شده و امکان ساخت یا فعال‌سازی کاربر جدید را ندارید.

نگران کاربرهایتان نباشید: هیچ‌چیز حذف نشده و به‌محض ثبت و تأیید پرداخت، پنل و سقف کاربران‌تان دقیقاً مثل قبل و به‌صورت خودکار برمی‌گردد.

مبلغ دقیق و راه‌های پرداخت در ربات برای شما ثبت شده است. اگر پرداخت را انجام داده‌اید و هنوز تأیید نشده، یا برای تسویه به چند روز مهلت نیاز دارید، همین‌جا خبر بدهید تا بررسی کنیم. 🙏`,

  frozen: `حساب نمایندگی شما فعلاً محدود شده است: کاربران فعلی‌تان آنلاین و سالم هستند، اما تا رفع محدودیت نمی‌توانید کاربر جدید بسازید.

این محدودیت با تسویهٔ فاکتور باز برداشته می‌شود و بعد از آن سقف کاربران‌تان دقیقاً به همان مقدار قبلی برمی‌گردد.

اگر پرداختی انجام داده‌اید یا سؤالی درباره‌اش دارید، همین‌جا پیام بدهید تا سریع رسیدگی کنم. 🙏`,

  debtor: `یادآوری می‌کنم که فاکتور دورهٔ شما هنوز تسویه نشده و از تاریخ سررسیدش گذشته است.

جزئیات کامل فاکتور (حجم فروخته‌شده و مبلغ) و راه‌های پرداخت در ربات موجود است؛ بعد از پرداخت کافی است رسید یا هش تراکنش را در همان‌جا ثبت کنید تا تأیید شود.

اگر مبلغ فاکتور برایتان محل سؤال است یا برای تسویه به مهلت نیاز دارید، لطفاً همین امروز خبر بدهید تا با هم هماهنگ کنیم و حساب‌تان محدود نشود. 🙏`,

  churned: `مدتی است سرویس جدیدی روی پنل شما ثبت نشده و نبودتان به چشم می‌آید.

اگر مشکلی در کیفیت سرویس، قیمت یا پشتیبانی باعثش شده، واقعاً ممنون می‌شوم رک بگویید — بازخورد شما برای ما ارزش دارد و بیشتر ایرادها قابل حل است.

اگر هم فقط فرصت نکرده‌اید، پنل‌تان همچنان فعال و آماده است و برای شروع دوباره کنارتان هستیم. یک پیام کوتاه بدهید تا وضعیت را با هم مرور کنیم. 🙏`,

  never_active: `پنل نمایندگی شما فعال است، اما تا امروز هیچ سرویسی روی آن ساخته نشده.

اگر در شروع کار سؤال یا مشکلی دارید — ساخت کاربر، تعیین حجم و مدت، گرفتن لینک اتصال، یا نحوهٔ تحویل سرویس به مشتری — بگویید تا قدم‌به‌قدم راهنمایی‌تان کنم.

برای شروع می‌توانید یک کانفیگ تست کم‌حجم بسازید و خودتان کیفیت را ببینید. منتظر پیام‌تان هستم. 🙏`,

  onboarding: `به جمع نمایندگان ما خوش آمدید 🌟
پنل شما فعال است و از این به بعد در کنارتان هستیم.

چند نکته برای شروع:
• ساخت کاربر، تعیین حجم و مدت، و گرفتن لینک اتصال، همه از خود پنل انجام می‌شود.
• فاکتور شما ماهانه و بر اساس مجموع حجمی که در آن ماه فروخته‌اید صادر و در همین ربات ارسال می‌شود.
• بعد از پرداخت، رسید یا هش تراکنش را در ربات ثبت می‌کنید تا تأیید شود.

هر جای کار سؤال داشتید بی‌تعارف پیام بدهید — برای همین اینجاییم. 🙏`,

  dormant: `چند وقتی است سرویس جدیدی روی پنل‌تان ثبت نشده؛ خواستم احوالی بپرسم و ببینم همه‌چیز مرتب است.

اگر مشکلی در کیفیت، سرعت یا پشتیبانی هست، بگویید تا سریع بررسی کنیم. اگر هم فقط سرتان شلوغ بوده، پنل و ظرفیت‌تان همچنان آماده است.

هر سؤالی داشتید در خدمتم. 🙏`,

  declining: `فروش این ماه شما نسبت به روند ماه‌های گذشته کمتر شده و خواستم ببینم از سمت ما مشکلی هست یا نه.

اگر از کیفیت یا قطعی سرویس شکایتی داشته‌اید، بگویید تا روی همان سرور بررسی کنیم. اگر هم فشار قیمت و رقابت بازار باعثش شده، درباره‌اش صحبت کنیم؛ معمولاً راهی پیدا می‌شود.

نظر شما برای ما مهم است. 🙏`,

  growing: `روند فروش شما این ماه رشد خوبی داشته — دستتان درد نکند 🌟

اگر برای ادامهٔ این رشد به ظرفیت بیشتر، سقف کاربر بالاتر یا هماهنگی خاصی نیاز دارید، بگویید تا برایتان تنظیم کنیم.

و اگر جایی از سرویس نیاز به بهبود دارد، همین حالا بگویید تا قبل از بزرگ‌تر شدن کار حلش کنیم. موفق باشید. 🙏`,

  healthy: `خواستم یک احوال‌پرسی ساده کرده باشم و بابت همکاری منظم‌تان تشکر کنم.

اگر نکته‌ای برای بهتر شدن سرویس یا پشتیبانی به ذهنتان می‌رسد، خوشحال می‌شوم بشنوم؛ و اگر به ظرفیت بیشتر یا هماهنگی خاصی نیاز داشتید، در خدمتم.

موفق و پرفروش باشید. 🙏`,
};

/** When to send it — one line of context above the text, so the owner never pastes the
 * win-back message into a debtor's chat. */
export const SEGMENT_MESSAGE_HINTS: Record<CrmSegment, string> = {
  suspended: "لحن آرام و راه‌حل‌محور: مسدودی برداشتنی است و چیزی از دست نرفته.",
  frozen: "توضیح می‌دهد چه چیزی محدود شده و چه چیزی هنوز کار می‌کند.",
  debtor: "یادآوری محترمانهٔ بدهی، قبل از اینکه به مرحلهٔ مسدودی برسد.",
  churned: "پیام بازگشت: علت رفتن را می‌پرسد و در را باز می‌گذارد.",
  never_active: "پیام راه‌اندازی: کمک برای ساختن اولین سرویس.",
  onboarding: "خوش‌آمدگویی و سه نکتهٔ اول کار.",
  dormant: "احوال‌پرسی سبک، قبل از اینکه به ریزش تبدیل شود.",
  declining: "علت افت این ماه را می‌پرسد، بدون سرزنش.",
  growing: "قدردانی + پیشنهاد ظرفیت بیشتر برای ادامهٔ رشد.",
  healthy: "تماس نگه‌داشتن رابطه؛ نه طلبی، نه گلایه‌ای.",
};

/** The full text to copy. `name` personalizes the greeting for a single reseller (the
 * drawer); the board copies the neutral form because one text goes to the whole bucket. */
export function segmentMessage(segment: string, name?: string): string {
  const body = SEGMENT_MESSAGES[segment as CrmSegment];
  if (!body) return "";
  const greeting = name?.trim() ? greetingFor(name.trim()) : GREETING_BULK;
  return `${greeting}\n${body}`;
}
