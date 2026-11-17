export const fmtToman = (n: number) =>
  `${Math.round(n || 0).toLocaleString("fa-IR")} تومان`;

// Show real (possibly fractional) GB — e.g. 0.5 GB must not round to "۱ گیگابایت".
export const fmtGb = (n: number) => {
  const v = n || 0;
  const s = Number.isInteger(v)
    ? v.toLocaleString("fa-IR")
    : v.toLocaleString("fa-IR", { maximumFractionDigits: 2 });
  return `${s} گیگابایت`;
};

export const fmtNum = (n: number) => (n || 0).toLocaleString("fa-IR");

export const fmtDate = (s?: string | null) => {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleDateString("fa-IR");
  } catch {
    return s;
  }
};

// Gregorian date + time in Iran time (Asia/Tehran), e.g. "2026-06-04 16:14". Used where
// the exact moment matters (panel sync), so the owner sees a real wall-clock in their zone.
export const fmtDateTime = (s?: string | null) => {
  if (!s) return "—";
  try {
    const d = new Date(s);
    const date = d.toLocaleDateString("en-CA", { timeZone: "Asia/Tehran" }); // YYYY-MM-DD
    const time = d.toLocaleTimeString("en-GB", {
      timeZone: "Asia/Tehran", hour: "2-digit", minute: "2-digit",
    });
    return `${date} ${time}`;
  } catch {
    return s;
  }
};

export const INVOICE_STATUS_FA: Record<string, string> = {
  draft: "پیش‌نویس؛ صادر نشده",
  sent: "صادرشده؛ پرداخت‌نشده",
  paid: "پرداخت‌شده",
  overdue: "سررسید گذشته",
  enforced: "بدهی معوق؛ تعلیق مالی",
  canceled: "لغوشده",
};

export const DELIVERY_STATUS_FA: Record<string, string> = {
  pending: "در انتظار ارسال تلگرام",
  sent: "ارسال موفق تلگرام",
  unmatched: "ارسال‌نشده؛ ثبت‌نام ربات ناقص",
  blocked: "ارسال‌نشده؛ ربات مسدود شده",
  failed: "ارسال ناموفق؛ تلاش مجدد لازم است",
};

export const PAYMENT_STATUS_FA: Record<string, string> = {
  pending: "در انتظار",
  confirmed: "تأییدشده",
  rejected: "ردشده",
};

export const PAYMENT_METHOD_FA: Record<string, string> = {
  usdt_txid: "USDT (شناسهٔ تراکنش)",
  ton_txid: "گرام (GRAM)",
  avax_txid: "AVAX (شناسهٔ تراکنش)",
  manual: "دستی",
  screenshot: "رسید تصویری",
};
