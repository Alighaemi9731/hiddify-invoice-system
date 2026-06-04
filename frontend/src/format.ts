export const fmtToman = (n: number) =>
  `${Math.round(n || 0).toLocaleString("fa-IR")} تومان`;

export const fmtUsdt = (n: number) =>
  `${(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`;

// Show real (possibly fractional) GB — e.g. 0.5 GB must not round to "۱ گیگ".
export const fmtGb = (n: number) => {
  const v = n || 0;
  const s = Number.isInteger(v)
    ? v.toLocaleString("fa-IR")
    : v.toLocaleString("fa-IR", { maximumFractionDigits: 2 });
  return `${s} گیگ`;
};

export const fmtNum = (n: number) => (n || 0).toLocaleString("fa-IR");

// Compact axis labels: 1.2M / 850K / 320
export const fmtCompact = (n: number) => {
  const a = Math.abs(n || 0);
  if (a >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(0) + "K";
  return String(Math.round(n || 0));
};

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
  draft: "پیش‌نویس",
  sent: "ارسال‌شده",
  paid: "پرداخت‌شده",
  overdue: "سررسید گذشته",
  enforced: "مسدود",
  canceled: "لغو",
};

export const PAYMENT_STATUS_FA: Record<string, string> = {
  pending: "در انتظار",
  confirmed: "تأییدشده",
  rejected: "ردشده",
  duplicate: "تکراری",
};

export const PAYMENT_METHOD_FA: Record<string, string> = {
  usdt_txid: "شناسهٔ تراکنش",
  usdt_hd: "آدرس اختصاصی",
  manual: "دستی",
  screenshot: "رسید تصویری",
};
