import { useState } from "react";
import {
  Box, Card, CardContent, Typography, Accordion, AccordionSummary, AccordionDetails,
  List, ListItem, ListItemIcon, ListItemText, Chip, Stack, TextField, Divider, Tabs, Tab,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import DashboardIcon from "@mui/icons-material/Dashboard";
import DnsIcon from "@mui/icons-material/Dns";
import GroupIcon from "@mui/icons-material/Group";
import ReceiptLongIcon from "@mui/icons-material/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/Payments";
import MoneyOffIcon from "@mui/icons-material/MoneyOff";
import BarChartIcon from "@mui/icons-material/BarChart";
import CampaignIcon from "@mui/icons-material/Campaign";
import HistoryIcon from "@mui/icons-material/History";
import ManageAccountsIcon from "@mui/icons-material/ManageAccounts";
import SettingsIcon from "@mui/icons-material/Settings";
import SmartToyIcon from "@mui/icons-material/SmartToy";
import SupportAgentIcon from "@mui/icons-material/SupportAgent";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import SearchIcon from "@mui/icons-material/Search";

type Section = {
  id: string;
  title: string;
  icon: any;
  intro?: string;
  items: { t: string; d: string; tag?: string }[];
};

const PANEL: Section[] = [
  {
    id: "dashboard", title: "داشبورد", icon: <DashboardIcon />,
    intro: "نمای کلی کسب‌وکار در یک صفحه؛ با انتخاب دوره (ماه) همهٔ آمار همان دوره را می‌بینید.",
    items: [
      { t: "کارت‌های آماری", d: "تعداد پنل‌ها، تعداد نمایندگان (و چند نفر به ربات متصل‌اند)، فروش دوره (تومان و معادل USDT) و مجموع بدهی معوق." },
      { t: "نمودار فروش بر اساس پنل", d: "میله‌ای مقایسه‌ای فروش هر پنل در دورهٔ انتخابی." },
      { t: "نمودار وضعیت فاکتورها", d: "دوناتِ توزیع فاکتورها بین وضعیت‌ها (پیش‌نویس، ارسال‌شده، پرداخت‌شده، …)." },
      { t: "۱۰ نمایندهٔ برتر دوره", d: "بزرگ‌ترین فروشنده‌های همان دوره به‌ترتیب مبلغ." },
      { t: "همگام‌سازی پنل‌ها", d: "کشیدن آخرین داده‌های همهٔ پنل‌ها در پس‌زمینه (دکمه فوری برمی‌گردد و وضعیت بعداً به‌روز می‌شود)." },
      { t: "اجرای یادآوری‌ها", d: "اجرای دستیِ چرخهٔ یادآوری/اخطار/مسدودسازی برای فاکتورهای پرداخت‌نشده." },
      { t: "صدور و ارسال ماهانه", d: "همگام‌سازی + صدور فاکتورهای دورهٔ انتخابی + ارسال آن‌ها در یک کلیک." },
    ],
  },
  {
    id: "panels", title: "پنل‌ها", icon: <DnsIcon />,
    intro: "پنل‌های هیدیفای خود را اینجا متصل می‌کنید (تا ۱۰ پنل).",
    items: [
      { t: "افزودن با لینک", d: "کافی است لینک ادمین پنل را بچسبانید؛ دامنه، مسیر مخفی و UUID مالک خودکار پر می‌شوند." },
      { t: "تست اتصال", d: "بدون ذخیره، یک‌بار بکاپ را می‌کشد تا صحت اتصال و اعتبارنامه را تأیید کند (تعداد ادمین/کاربر را نشان می‌دهد)." },
      { t: "همگام‌سازی", d: "داده‌های پنل (ادمین‌ها و کاربران) را در پس‌زمینه وارد سامانه می‌کند؛ وضعیت «در حال همگام‌سازی…» سپس «موفق» می‌شود." },
      { t: "کلید API ادمین", d: "اختیاری؛ فقط برای مسدودسازی واقعی لازم است. اگر خالی بگذارید از UUID مالک استفاده می‌شود." },
      { t: "ویرایش/حذف", d: "تغییر دامنه/مسیر/کلید یا حذف پنل. هنگام ویرایش، فیلدهای حساس را خالی بگذارید تا مقدار قبلی حفظ شود." },
    ],
  },
  {
    id: "resellers", title: "نمایندگان", icon: <GroupIcon />,
    intro: "همهٔ ادمین‌های پنل (نمایندگان) و زیرمجموعه‌هایشان.",
    items: [
      { t: "نمای فهرست", d: "جدول قابل‌مرتب‌سازی با نام، پنل، قیمت هر گیگ، اتصال به ربات، وضعیت و معافیت." },
      { t: "نمای ساختار درختی", d: "هر نمایندهٔ اصلی با تعداد زیرمجموعه‌ها؛ با باز کردن، زیرمجموعه‌ها داخل همان نماینده دیده می‌شوند." },
      { t: "قیمت اختصاصی هر گیگ", d: "اگر خالی بماند از قیمت پیش‌فرض تنظیمات استفاده می‌شود." },
      { t: "حداقل فروش", d: "کمترین مبلغ مجاز برای کل مجموعهٔ یک نماینده (خودش + زیرمجموعه‌ها). فروش زیر این حد ولی بالای صفر، به همین مبلغ گرد می‌شود." },
      { t: "معاف از صدور فاکتور", d: "نماینده‌هایی که نباید برایشان فاکتور صادر شود (مثلاً حساب‌های داخلی)." },
      { t: "مسدودسازی / بازگردانی", d: "غیرفعال‌سازی کاربران نماینده و صفر کردن سقف او؛ بازگردانی دقیقاً مقادیر قبلی را برمی‌گرداند." },
    ],
  },
  {
    id: "invoices", title: "فاکتورها", icon: <ReceiptLongIcon />,
    intro: "صدور، ارسال، ویرایش و مدیریت فاکتورهای ماهانه.",
    items: [
      { t: "صدور فاکتورهای دوره", d: "محاسبهٔ فاکتور هر نماینده برای دورهٔ انتخابی (ماه میلادی). فاکتور هر نماینده شامل فروش خودش و همهٔ زیرمجموعه‌هایش است." },
      { t: "ارسال (تکی/همگانی)", d: "ارسال فاکتور به ربات نماینده؛ متن + فایل PDF در یک پیام." },
      { t: "PDF حرفه‌ای", d: "فاکتور فارسی/RTL با جدول سرویس‌ها، شناسهٔ کوتاه هر سرویس، تاریخ ساخت و مبلغ به تومان و USDT." },
      { t: "ویرایش دستی", d: "اصلاح مصرف/قیمت یک فاکتور و ارسال مجدد؛ مبلغ و USDT بازمحاسبه می‌شوند." },
      { t: "ثبت/لغو پرداخت", d: "علامت‌گذاری دستی فاکتور به‌عنوان پرداخت‌شده و برگرداندن آن در صورت اشتباه." },
      { t: "مهلت پرداخت", d: "تعیین مهلت برای یک فاکتور؛ تا آن تاریخ یادآوری و مسدودسازی متوقف می‌شود (بدون اثر بر بقیه)." },
      { t: "تب فاکتور صفر", d: "نمایندگانی که در این دوره هیچ فروشی نداشته‌اند." },
    ],
  },
  {
    id: "payments", title: "پرداخت‌ها", icon: <PaymentsIcon />,
    intro: "پرداخت‌های USDT (شبکه BEP-20).",
    items: [
      { t: "بررسی زنجیره (TXID)", d: "تأیید on-chain تراکنشی که نماینده در ربات ثبت کرده (مقصد، مبلغ، تعداد تأیید) و علامت خودکار پرداخت." },
      { t: "تأیید/رد دستی", d: "اگر خواستید بدون بررسی زنجیره، پرداخت را دستی تأیید یا رد کنید." },
      { t: "بازگردانی خودکار", d: "با تأیید پرداخت، اگر نماینده قبلاً مسدود شده بود، دسترسی‌اش خودکار برمی‌گردد." },
    ],
  },
  {
    id: "debts", title: "بدهی‌ها", icon: <MoneyOffIcon />,
    items: [
      { t: "فهرست بدهکاران", d: "نمایندگانی با فاکتور پرداخت‌نشده، مبلغ معوق (تومان/USDT)، اتصال به ربات و قدیمی‌ترین دوره." },
    ],
  },
  {
    id: "sales", title: "فروش نمایندگان", icon: <BarChartIcon />,
    items: [
      { t: "گزارش فروش دوره", d: "جدول قابل‌مرتب‌سازی فروش همهٔ نمایندگان در دورهٔ انتخابی با مجموع کل." },
    ],
  },
  {
    id: "broadcast", title: "پیام همگانی", icon: <CampaignIcon />,
    intro: "ارسال پیام به گروه‌های مختلف نمایندگان از طریق ربات.",
    items: [
      { t: "گروه‌بندی گیرندگان", d: "همه نمایندگان، فقط بدهکاران، فروش صفر این ماه، یا نمایندگان یک پنل خاص." },
      { t: "پاک‌سازی کانال", d: "حذف افرادی که ربات را استارت زده‌اند ولی نماینده نیستند، از کانال اطلاع‌رسانی (پیش‌فرض حالت آزمایشی)." },
    ],
  },
  {
    id: "logs", title: "گزارش‌ها", icon: <HistoryIcon />,
    items: [
      { t: "گزارش ارسال پیام‌ها", d: "وضعیت تحویل هر فاکتور/یادآوری به نماینده (موفق، ناموفق، بدون ربات، مسدود)." },
      { t: "گزارش مسدودسازی", d: "تاریخچهٔ اقدامات مسدودسازی/بازگردانی، تعداد کاربران متأثر و حالت آزمایشی/واقعی." },
    ],
  },
  {
    id: "account", title: "حساب و پشتیبان", icon: <ManageAccountsIcon />,
    intro: "مدیریت حساب مدیر و پشتیبان‌گیری/بازیابی کامل سامانه.",
    items: [
      { t: "تغییر نام کاربری و رمز", d: "با وارد کردن رمز فعلی، نام کاربری و/یا رمز را تغییر دهید." },
      { t: "پشتیبان خودکار", d: "هر ۲ ساعت یک فایل پشتیبان کامل (دیتابیس + تنظیمات) به پی‌وی تلگرام مدیر ارسال می‌شود." },
      { t: "دانلود/ارسال پشتیبان", d: "گرفتن پشتیبان همین حالا (دانلود مستقیم یا ارسال به تلگرام)." },
      { t: "بازیابی", d: "بارگذاری فایل پشتیبان برای بازگرداندن کل سامانه (یا ارسال همان فایل به ربات)." },
    ],
  },
  {
    id: "settings", title: "تنظیمات", icon: <SettingsIcon />,
    intro: "همهٔ پیکربندی‌های قابل‌ویرایش (مقادیر حساس رمزنگاری می‌شوند).",
    items: [
      { t: "تلگرام", d: "توکن ربات، کانال اطلاع‌رسانی، لینک عضویت یکبارمصرف و مسدودسازی واقعی کانال." },
      { t: "پرداخت", d: "آدرس کیف پول USDT، قرارداد، کلید BscScan، حداقل تأیید و xpub کیف پول مادر." },
      { t: "قیمت‌گذاری", d: "قیمت پیش‌فرض هر گیگ، نرخ تبدیل تومان به USDT، حجم‌های معاف (تست) و حداقل فروش." },
      { t: "زمان‌بندی و یادآوری", d: "روزهای یادآوری (D+۲/D+۴)، اخطار و مسدودسازی، و کلید فعال‌سازی مسدودسازی واقعی." },
      { t: "متن پیام‌ها", d: "قالب همهٔ پیام‌های ربات (خوش‌آمد، فاکتور، یادآوری، اخطار، …) با جای‌گذاری خودکار مقادیر." },
      { t: "دامنه و HTTPS", d: "برای نصب روی سرور (فاز ۲): دامنه و گواهی SSL خودکار." },
    ],
  },
];

const ADMIN_BOT: Section[] = [
  {
    id: "admin-bot", title: "ربات — مدیر", icon: <SmartToyIcon />,
    intro: "با اکانت مدیر، منوی `/` فقط دستورات مدیریتی را نشان می‌دهد.",
    items: [
      { t: "📊 آمار کلی", d: "خلاصهٔ پنل‌ها، نمایندگان، فروش دورهٔ جاری و بدهی معوق." },
      { t: "💰 بدهکاران", d: "فهرست بدهکاران برتر با مبلغ." },
      { t: "📢 پیام همگانی", d: "انتخاب گروه گیرنده (همه/بدهکاران/فروش صفر) و ارسال پیام.", tag: "/broadcast" },
      { t: "تنظیم کانال", d: "یک پست از کانال را برای ربات فوروارد کنید تا کانال اطلاع‌رسانی ثبت شود." },
      { t: "بازیابی با فایل", d: "ارسال فایل zip پشتیبان به ربات، سامانه را بازیابی می‌کند." },
      { t: "پاسخ به پشتیبانی", d: "روی «پاسخ» زیر پیام کاربر بزنید؛ جواب به‌صورت ریپلای به همان پیام او می‌رسد." },
      { t: "گزارش خودکار", d: "بعد از صدور ماهانه یا مسدودسازی، خلاصه به پی‌وی شما می‌آید؛ نماینده‌های مسدودشده لینک کلیک‌شونده دارند." },
    ],
  },
];

const RESELLER_BOT: Section[] = [
  {
    id: "reseller-bot", title: "ربات — نماینده", icon: <SupportAgentIcon />,
    intro: "نماینده ابتدا باید عضو کانال شود (با لینک یکبارمصرف)، سپس منو فعال می‌شود.",
    items: [
      { t: "🔗 ثبت لینک پنل", d: "نماینده لینک پنلش را می‌فرستد؛ با host+UUID به رکوردش وصل می‌شود (لینک نامرتبط رد می‌شود).", tag: "/start" },
      { t: "🖥 پنل‌های من", d: "فهرست پنل‌ها و تعداد زیرمجموعه‌های نماینده." },
      { t: "🧾 فاکتورهای من", d: "فاکتورهای اخیر با مبلغ و وضعیت.", tag: "/invoices" },
      { t: "💳 پرداخت", d: "آدرس کیف پول و مبلغ قابل پرداخت؛ سپس ارسال TXID برای تأیید.", tag: "/pay" },
      { t: "📊 بدهی من", d: "مجموع بدهی پرداخت‌نشده.", tag: "/debt" },
      { t: "💬 پیام به پشتیبانی", d: "ارسال پیام به مدیر؛ پاسخ مدیر به‌صورت ریپلای برمی‌گردد." },
      { t: "🗑 حذف لینک‌های من", d: "حذف لینک‌های ثبت‌شده.", tag: "/removelink" },
    ],
  },
];

function SectionCard({ s }: { s: Section }) {
  return (
    <Accordion defaultExpanded={false} sx={{ mb: 1 }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1.25} alignItems="center">
          <Box sx={{ color: "primary.main", display: "flex" }}>{s.icon}</Box>
          <Typography sx={{ fontWeight: 700 }}>{s.title}</Typography>
          <Chip size="small" label={`${s.items.length} مورد`} variant="outlined" />
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        {s.intro && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>{s.intro}</Typography>
        )}
        <List dense disablePadding>
          {s.items.map((it, i) => (
            <ListItem key={i} alignItems="flex-start" sx={{ px: 0 }}>
              <ListItemIcon sx={{ minWidth: 34, mt: 0.5 }}>
                <CheckCircleOutlineIcon fontSize="small" color="success" />
              </ListItemIcon>
              <ListItemText
                primary={
                  <Stack direction="row" spacing={1} alignItems="center" component="span">
                    <Typography component="span" sx={{ fontWeight: 600 }}>{it.t}</Typography>
                    {it.tag && <Chip component="span" size="small" label={it.tag} dir="ltr"
                      sx={{ height: 20, fontFamily: "monospace" }} />}
                  </Stack>
                }
                secondary={it.d}
              />
            </ListItem>
          ))}
        </List>
      </AccordionDetails>
    </Accordion>
  );
}

const GROUPS: { label: string; data: Section[] }[] = [
  { label: "پنل مدیریت", data: PANEL },
  { label: "ربات مدیر", data: ADMIN_BOT },
  { label: "ربات نمایندگان", data: RESELLER_BOT },
];

export default function Help() {
  const [tab, setTab] = useState(0);
  const [q, setQ] = useState("");
  const ql = q.trim().toLowerCase();

  const filterSec = (s: Section): Section | null => {
    if (!ql) return s;
    if (s.title.toLowerCase().includes(ql)) return s;
    const items = s.items.filter((i) => i.t.toLowerCase().includes(ql) || i.d.toLowerCase().includes(ql));
    return items.length ? { ...s, items } : null;
  };

  const sections = GROUPS[tab].data.map(filterSec).filter(Boolean) as Section[];

  return (
    <Box>
      <Card sx={{ mb: 2, bgcolor: (t) => alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.18 : 0.06) }}>
        <CardContent>
          <Stack direction="row" spacing={1.5} alignItems="center">
            <HelpOutlineIcon color="primary" sx={{ fontSize: 34 }} />
            <Box>
              <Typography variant="h6" sx={{ fontWeight: 800 }}>راهنمای کامل سامانه</Typography>
              <Typography variant="body2" color="text.secondary">
                همهٔ امکانات پنل مدیریت، ربات مدیر و ربات نمایندگان — دسته‌بندی‌شده و قابل‌جستجو.
              </Typography>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }} alignItems="center">
        <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ flexGrow: 1 }}>
          {GROUPS.map((g) => <Tab key={g.label} label={g.label} />)}
        </Tabs>
        <TextField size="small" placeholder="جستجو در راهنما…" value={q}
          onChange={(e) => setQ(e.target.value)}
          InputProps={{ startAdornment: <SearchIcon fontSize="small" sx={{ ml: 1, color: "text.secondary" }} /> }} />
      </Stack>

      <Divider sx={{ mb: 2 }} />

      {sections.length === 0 ? (
        <Typography color="text.secondary" sx={{ p: 3, textAlign: "center" }}>موردی یافت نشد.</Typography>
      ) : (
        sections.map((s) => <SectionCard key={s.id} s={s} />)
      )}
    </Box>
  );
}
