import { Chip, Stack, Typography } from "@mui/material";
import { DELIVERY_STATUS_FA, fmtDate, fmtDateTime } from "../format";

export function InvoiceDelivery({ invoice }: { invoice: {
  status: string; sent_at?: string | null; telegram_delivery_status?: string | null;
} }) {
  const status = invoice.telegram_delivery_status || (invoice.sent_at ? "sent" : "pending");
  return <Stack spacing={0.4} alignItems="flex-start">
    <Chip size="small" variant="outlined" color={status === "sent" ? "success" : status === "pending" ? "default" : "warning"}
      label={invoice.status === "draft" ? "هنوز صادر نشده" : DELIVERY_STATUS_FA[status] || "در انتظار ارسال"} />
    {invoice.sent_at && <Typography variant="caption" color="text.secondary">اولین ارسال: {fmtDate(invoice.sent_at)}</Typography>}
  </Stack>;
}

export function InvoiceDeadlines({ invoice }: { invoice: {
  due_on?: string | null; enforcement_on?: string | null;
} }) {
  return <Stack spacing={0.3}>
    <Typography variant="caption">سررسید: {fmtDate(invoice.due_on)}</Typography>
    <Typography variant="caption" color="text.secondary">تعلیق مالی: {fmtDate(invoice.enforcement_on)}</Typography>
  </Stack>;
}

export function InvoiceAccess({ invoice }: { invoice: {
  reseller_access_state?: string | null; registration_blocked?: boolean;
  registration_deadline_at?: string | null;
} }) {
  const suspended = invoice.reseller_access_state === "enforced";
  return <Stack spacing={0.4} alignItems="flex-start">
    <Chip size="small" color={suspended ? "error" : "default"} variant="outlined"
      label={suspended ? "سرویس تعلیق‌شده" : invoice.reseller_access_state === "frozen" ? "ساخت کاربر محدود" : invoice.reseller_access_state === "active" ? "سرویس فعال" : "وضعیت دسترسی نامشخص"} />
    {invoice.registration_blocked
      ? <Typography variant="caption" color="error">ثبت‌نام ناقص</Typography>
      : invoice.registration_deadline_at && <Typography variant="caption" color="text.secondary">
        مهلت ثبت‌نام: {fmtDateTime(invoice.registration_deadline_at)}
      </Typography>}
  </Stack>;
}
