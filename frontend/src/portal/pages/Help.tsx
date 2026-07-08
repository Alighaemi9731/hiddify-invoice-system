import { Box, Card, CardContent, Stack, Typography } from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import DashboardIcon from "@mui/icons-material/esm/Dashboard";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import GroupIcon from "@mui/icons-material/esm/Group";
import DnsIcon from "@mui/icons-material/esm/Dns";
import SupportAgentIcon from "@mui/icons-material/esm/SupportAgent";
import { ReactNode } from "react";

const SECTIONS: { icon: ReactNode; color: string; title: string; body: string[] }[] = [
  {
    icon: <DashboardIcon />, color: "#0071e3", title: "داشبورد",
    body: [
      "برآوردِ فروشِ ماهِ جاری (خودتان + همهٔ زیرمجموعه‌ها)، حجمِ فروخته‌شده، تعدادِ سرویس و بدهیِ معوق.",
      "نمودارِ «روندِ فروشِ روزانه» سهمِ هر روز از فروشِ ماه را نشان می‌دهد.",
    ],
  },
  {
    icon: <ReceiptLongIcon />, color: "#f59e0b", title: "فاکتورها",
    body: [
      "همهٔ فاکتورهای صادرشده‌تان؛ فاکتورهای پرداخت‌نشده با برچسبِ «بدهکار» مشخص‌اند.",
      "از دکمهٔ PDF می‌توانید نسخهٔ حجمیِ هر فاکتور را دانلود کنید.",
      "برای پرداخت، روی «پرداخت» بزنید و طبقِ راهنمای پرداخت (USDT/گرام‌TON/AVAX/کارت) عمل کنید.",
    ],
  },
  {
    icon: <PaymentsIcon />, color: "#10b981", title: "پرداخت‌ها",
    body: [
      "تاریخچهٔ پرداخت‌ها، روندِ بررسی (ثبت → بررسی → نتیجه) و رسیدِ هرکدام.",
      "برای USDT/گرام‌TON/AVAX «شناسهٔ تراکنش (TXID)» و برای کارت «تصویرِ رسید» را در دیالوگِ پرداخت بفرستید (شبکه را از فهرست انتخاب کنید).",
      "هر پرداخت را مالک به‌صورتِ دستی تأیید می‌کند؛ نتیجه همین‌جا و در ربات به شما اطلاع داده می‌شود.",
    ],
  },
  {
    icon: <GroupIcon />, color: "#22c55e", title: "زیرمجموعه‌ها",
    body: [
      "ظرفیتِ کاربران (ساخته‌شده / سقف)، فروشِ ماهانه و سقفِ حجمِ هر زیرمجموعه.",
      "می‌توانید سقفِ حجمِ ماهانه تعیین کنید، ظرفیتِ کاربرشان را افزایش دهید، اجازهٔ ساختِ زیرمجموعه بدهید، PDF بگیرید و در صورتِ نیاز مسدود/آزاد کنید.",
    ],
  },
  {
    icon: <DnsIcon />, color: "#0ea5e9", title: "پنل‌ها و ظرفیت",
    body: [
      "لینکِ مدیریتِ شما روی هر پنل (با دکمهٔ کپی).",
      "بخشِ «ظرفیتِ من» میزانِ پُریِ سهمیهٔ کاربرانتان را نشان می‌دهد؛ با «درخواستِ افزایش» می‌توانید از مالک ظرفیتِ بیشتر بخواهید.",
    ],
  },
  {
    icon: <SupportAgentIcon />, color: "#a855f7", title: "پشتیبانی",
    body: [
      "پیامتان مستقیم به پشتیبانی می‌رسد و پاسخ از طریقِ ربات تلگرامِ شما ارسال می‌شود.",
      "زنگوله‌ٔ بالای صفحه اعلان‌های مهم (فاکتور، نتیجهٔ پرداخت، مسدودی، سقفِ حجم) را نشان می‌دهد.",
    ],
  },
];

export default function PortalHelp() {
  const theme = useTheme();
  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 0.4 }}>راهنما</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        راهنمای کوتاهِ بخش‌های پنلِ نماینده
      </Typography>

      <Stack spacing={2}>
        {SECTIONS.map((s) => (
          <Card key={s.title}>
            <CardContent>
              <Stack direction="row" spacing={1.3} alignItems="center" sx={{ mb: 1 }}>
                <Box sx={{
                  width: 36, height: 36, borderRadius: 2, display: "grid", placeItems: "center",
                  color: s.color, bgcolor: alpha(s.color, theme.palette.mode === "dark" ? 0.22 : 0.14),
                  "& svg": { fontSize: 20 },
                }}>{s.icon}</Box>
                <Typography variant="subtitle1" sx={{ fontWeight: 800 }}>{s.title}</Typography>
              </Stack>
              <Stack component="ul" sx={{ m: 0, pr: 2.5, pl: 0, color: "text.secondary" }} spacing={0.6}>
                {s.body.map((line, i) => (
                  <li key={i}><Typography variant="body2">{line}</Typography></li>
                ))}
              </Stack>
            </CardContent>
          </Card>
        ))}
      </Stack>
    </Box>
  );
}
