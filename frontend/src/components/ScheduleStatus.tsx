import { Alert, Box, Button, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { getScheduleStatus } from "../api/client";
import { fmtDateTime, fmtNum } from "../format";

export default function ScheduleStatus() {
  const { data, isError, refetch } = useQuery({
    queryKey: ["schedule-status"], queryFn: getScheduleStatus, refetchInterval: 5000,
  });
  if (isError) return <Alert severity="warning" sx={{ mb: 2 }}
    action={<Button color="inherit" onClick={() => refetch()}>تلاش دوباره</Button>}>
    دریافت وضعیت زمان‌بندی انجام نشد.
  </Alert>;
  if (!data) return <Typography role="status" sx={{ mb: 2 }}>در حال دریافت وضعیت زمان‌بندی…</Typography>;
  const applied = data.status === "applied";
  const run = data.monthly_run;
  return <Box sx={{ mb: 3 }} aria-label="وضعیت زمان‌بندی">
    <Stack spacing={1}>
      <Alert severity={applied ? "success" : "warning"} role="status">
        {applied ? "زمان‌بندی ذخیره‌شده اعمال شده است."
          : data.status === "pending" ? "در انتظار اعمال تغییرات؛ معمولاً تا ۱۵ ثانیه."
            : "وضعیت زمان‌بند به‌روز نیست؛ اجرای خودکار نیاز به بررسی دارد."}
      </Alert>
      <Typography variant="body2">
        برنامهٔ ذخیره‌شده: روز {fmtNum(data.desired.invoice_day)} هر ماه میلادی، ساعت {fmtNum(data.desired.invoice_hour)} به وقت تهران
      </Typography>
      {!applied && data.active && <Typography variant="body2">
        آخرین برنامهٔ فعال: روز {fmtNum(data.active.invoice_day)}، ساعت {fmtNum(data.active.invoice_hour)}
      </Typography>}
      {data.next_invoice_at && <Typography variant="body2">
        نوبت بعدی صدور ماهانه: <Box component="span" dir="ltr">{fmtDateTime(data.next_invoice_at)}</Box> (تهران)
      </Typography>}
      {run && <Typography variant="body2">
        دورهٔ <Box component="span" dir="ltr">{run.period}</Box>: {{
          running: "صدور و ارسال در حال انجام است", completed: "اجرای ماهانه انجام شده است",
          retry: "ارسال کامل نشده؛ تلاش مجدد خودکار انجام می‌شود", failed: "اجرای ماهانه نیاز به بررسی دارد",
        }[run.status] ?? "در انتظار بررسی"}
      </Typography>}
      <Typography variant="body2" color="text.secondary">
        نوبت ازدست‌رفتهٔ دورهٔ ماه قبل، پس از رسیدن موعد ذخیره‌شده، هر دقیقه بررسی و بازیابی می‌شود.
      </Typography>
    </Stack>
  </Box>;
}
