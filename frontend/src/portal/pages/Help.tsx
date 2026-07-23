import { useState, ReactNode } from "react";
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Card, CardContent, Chip, List, ListItem,
  ListItemIcon, ListItemText, Stack, Tab, Tabs, TextField, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import ExpandMoreIcon from "@mui/icons-material/esm/ExpandMore";
import CheckCircleOutlineIcon from "@mui/icons-material/esm/CheckCircleOutline";
import HelpOutlineIcon from "@mui/icons-material/esm/HelpOutline";
import DashboardIcon from "@mui/icons-material/esm/Dashboard";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import GroupIcon from "@mui/icons-material/esm/Group";
import DnsIcon from "@mui/icons-material/esm/Dns";
import SupportAgentIcon from "@mui/icons-material/esm/SupportAgent";
import StorefrontIcon from "@mui/icons-material/esm/Storefront";
import BarChartIcon from "@mui/icons-material/esm/BarChart";
import AccountBalanceWalletIcon from "@mui/icons-material/esm/AccountBalanceWallet";
import CampaignIcon from "@mui/icons-material/esm/Campaign";

type Item = { t: string; d: string; tag?: string };
type Section = { icon: ReactNode; title: string; intro?: string; items: Item[] };

// Same shape as the owner panel's Help (grouped tabs + search + accordions) so both pages read as
// one product instead of two different tools.
const MY_PANEL: Section[] = [
  {
    icon: <DashboardIcon />, title: "داشبورد",
    intro: "تصویرِ سریعِ وضعیتِ شما در ماهِ جاری.",
    items: [
      { t: "برآوردِ فروشِ ماه", d: "فروشِ خودتان به‌علاوهٔ همهٔ زیرمجموعه‌ها، حجمِ فروخته‌شده و تعدادِ سرویس." },
      { t: "بدهیِ معوق", d: "مجموعِ فاکتورهای پرداخت‌نشده‌تان؛ با کلیک می‌توانید مستقیم پرداخت کنید." },
      { t: "روندِ فروشِ روزانه", d: "سهمِ هر روز از فروشِ ماه، برای دیدنِ روزهای پرفروش و افت‌ها." },
    ],
  },
  {
    icon: <ReceiptLongIcon />, title: "فاکتورها",
    items: [
      { t: "فهرستِ فاکتورها", d: "همهٔ فاکتورهای صادرشده؛ پرداخت‌نشده‌ها با برچسبِ «بدهکار» مشخص‌اند." },
      { t: "دریافتِ PDF", d: "نسخهٔ حجمیِ هر فاکتور (فقط گیگابایت، بدون قیمت) — قابلِ تحویل به زیرمجموعه." },
      { t: "پرداخت", d: "روی «پرداخت» بزنید و طبقِ راهنما مبلغ را بفرستید." },
    ],
  },
  {
    icon: <PaymentsIcon />, title: "پرداخت‌ها",
    intro: "همهٔ پرداخت‌ها را مالک به‌صورتِ دستی تأیید می‌کند؛ نتیجه همین‌جا و در ربات به شما اطلاع داده می‌شود.",
    items: [
      { t: "رمزارز", d: "شناسهٔ تراکنش را بفرستید و شبکه را از فهرست انتخاب کنید.", tag: "USDT / TON / AVAX" },
      { t: "کارت‌به‌کارت", d: "تصویرِ رسید را در دیالوگِ پرداخت بارگذاری کنید." },
      { t: "پیگیری", d: "هر پرداخت یک شمارهٔ پیگیری دارد؛ مسیرِ ثبت → بررسی → نتیجه را همین‌جا می‌بینید." },
    ],
  },
  {
    icon: <GroupIcon />, title: "زیرمجموعه‌ها",
    items: [
      { t: "ظرفیت و فروش", d: "کاربرانِ ساخته‌شده در برابرِ سقف، و فروشِ ماهانهٔ هر زیرمجموعه." },
      { t: "سقفِ حجمِ ماهانه", d: "برای هر زیرمجموعه سقفِ گیگابایت تعیین کنید؛ هنگامِ رسیدن به سقف خبر می‌گیرید." },
      { t: "افزایشِ ظرفیت", d: "سقفِ تعدادِ کاربرِ زیرمجموعه را بالا ببرید یا اجازهٔ ساختِ زیرمجموعه بدهید." },
      { t: "توقف و مسدودسازی", d: "«توقفِ ساختِ کاربر» جلوی رشد را می‌گیرد ولی کاربرانِ فعلی آنلاین می‌مانند؛ «مسدودسازی» همه را قطع می‌کند." },
    ],
  },
  {
    icon: <DnsIcon />, title: "پنل‌ها و ظرفیت من",
    items: [
      { t: "لینکِ مدیریت", d: "لینکِ شما روی هر پنل، با دکمهٔ کپی." },
      { t: "ظرفیتِ من", d: "میزانِ پُریِ سهمیهٔ کاربرانتان؛ با «درخواستِ افزایش» از مالک ظرفیتِ بیشتر بخواهید." },
    ],
  },
  {
    icon: <SupportAgentIcon />, title: "پشتیبانی و اعلان‌ها",
    items: [
      { t: "پیام به پشتیبانی", d: "پیامتان مستقیم به مالک می‌رسد و پاسخ از طریقِ ربات تلگرامِ شما می‌آید." },
      { t: "زنگولهٔ اعلان‌ها", d: "فاکتورِ جدید، نتیجهٔ پرداخت، مسدودی و رسیدن به سقفِ حجم." },
      { t: "ورود به پورتال", d: "آدرسِ پورتالِ شما دائمی است و منقضی نمی‌شود؛ می‌توانید ذخیره‌اش کنید." },
    ],
  },
];

const MY_SHOP: Section[] = [
  {
    icon: <StorefrontIcon />, title: "شروع کار با فروشگاه",
    intro: "اگر ربات فروشگاهِ خودتان را دارید، همهٔ مدیریتِ روزمره در همین پورتال انجام می‌شود.",
    items: [
      { t: "ورود از ربات", d: "در ربات فروشگاه دکمهٔ «🌐 مدیریت فروشگاه در مرورگر» را بزنید تا بدون رمز وارد شوید." },
      { t: "کارهای فوری در ربات", d: "«شارژهای در انتظار»، «مشتری‌ها» و «پیام همگانی» در خودِ ربات هم هستند." },
      { t: "هم‌مدیرها", d: "هم‌مدیرها (co-admin) فروشگاه را از داخلِ ربات مدیریت می‌کنند؛ دسترسیِ تحتِ وبِ آن‌ها فعلاً فعال نیست." },
    ],
  },
  {
    icon: <BarChartIcon />, title: "داشبورد فروشگاه",
    items: [
      { t: "روندِ فروشِ روزانه", d: "فروشِ هر روزِ ماه، خالص از برگشتِ وجه." },
      { t: "پرفروش‌ترین پلن‌ها", d: "کدام پلن‌ها بیشترین درآمد و تعدادِ فروش را داشته‌اند." },
      { t: "مقایسه با ماه گذشته", d: "درصدِ رشد یا افتِ فروشِ این ماه نسبت به ماهِ قبل." },
      { t: "سرویس‌های نزدیکِ انقضا", d: "تعدادِ سرویس‌هایی که به‌زودی تمام می‌شوند — فرصتِ تمدید." },
    ],
  },
  {
    icon: <AccountBalanceWalletIcon />, title: "مشتری‌ها، کیف پول و شارژ",
    items: [
      { t: "مشتری‌ها", d: "جستجو، مشاهدهٔ سرویس‌ها، شارژ/کسرِ دستیِ کیفِ پول، پیامِ مستقیم و مسدودسازی." },
      { t: "تأییدِ شارژ", d: "شارژهای در انتظار را با مبلغِ دلخواه تأیید یا رد کنید؛ مشتری بلافاصله خبردار می‌شود." },
      { t: "کدهای شارژ و هدیه", d: "کدِ درصدی، مبلغِ ثابت یا هدیهٔ بدونِ پرداخت بسازید و مصرفشان را ببینید." },
    ],
  },
  {
    icon: <CampaignIcon />, title: "پلن‌ها، پیام‌ها و اطلاع‌رسانی",
    items: [
      { t: "پلن‌ها", d: "افزودن، ویرایش، ترتیب و فعال/غیرفعال کردنِ پلن‌ها." },
      { t: "پیامِ همگانی", d: "به همهٔ مشتری‌ها یا گروه‌های خاص (منقضی‌شده‌ها، تست‌گرفته‌ها) پیام بدهید." },
      { t: "یادآوریِ خودکار", d: "ربات به‌صورتِ خودکار هنگامِ نزدیک‌شدن به انقضا، پس از پایانِ تست، هنگامِ مصرفِ بالای حجم و پس از منقضی‌شدن به مشتری یادآوری می‌کند." },
    ],
  },
];

const GROUPS: { label: string; data: Section[] }[] = [
  { label: "پنلِ من", data: MY_PANEL },
  { label: "فروشگاهِ من", data: MY_SHOP },
];

export default function PortalHelp() {
  const [tab, setTab] = useState(0);
  const [q, setQ] = useState("");
  const ql = q.trim().toLowerCase();

  // While searching, look across ALL groups (not just the open tab) so nothing is missed.
  const filterSec = (s: Section): Section | null => {
    if (!ql) return s;
    if (s.title.toLowerCase().includes(ql)) return s;
    const items = s.items.filter((i) => i.t.toLowerCase().includes(ql) || i.d.toLowerCase().includes(ql));
    return items.length ? { ...s, items } : null;
  };
  const source = ql ? GROUPS.flatMap((g) => g.data) : GROUPS[tab].data;
  const sections = source.map(filterSec).filter(Boolean) as Section[];

  return (
    <Box>
      <Card
        sx={{
          mb: 2, border: "none",
          backgroundImage: (t) =>
            `linear-gradient(135deg,${alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.28 : 0.09)} 0%,transparent 70%)`,
        }}
      >
        <CardContent>
          <Stack direction="row" spacing={1.5} alignItems="center">
            <HelpOutlineIcon color="primary" sx={{ fontSize: 36 }} />
            <Box>
              <Typography variant="h6" sx={{ fontWeight: 800 }}>راهنمای پنلِ نمایندگان</Typography>
              <Typography variant="body2" color="text.secondary">
                همهٔ امکاناتِ پنلِ شما و مدیریتِ فروشگاه — دسته‌بندی‌شده و قابلِ جستجو.
              </Typography>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }} alignItems="center">
        <Tabs
          value={tab} onChange={(_, v) => setTab(v)} variant="scrollable" scrollButtons="auto"
          sx={{ flexGrow: 1 }} aria-disabled={!!ql}
        >
          {GROUPS.map((g) => <Tab key={g.label} label={g.label} />)}
        </Tabs>
        <TextField
          size="small" placeholder="جستجو در راهنما…" value={q}
          sx={{ minWidth: { xs: "100%", sm: 220 } }}
          onChange={(e) => setQ(e.target.value)}
        />
      </Stack>

      {sections.length === 0 ? (
        <Card><CardContent>
          <Typography color="text.secondary" sx={{ textAlign: "center", py: 3 }}>
            چیزی برای «{q}» پیدا نشد.
          </Typography>
        </CardContent></Card>
      ) : (
        sections.map((s) => <HelpSection key={s.title} s={s} />)
      )}
    </Box>
  );
}

function HelpSection({ s }: { s: Section }) {
  return (
    <Accordion
      disableGutters defaultExpanded
      sx={{ mb: 1, "&:before": { display: "none" }, overflow: "hidden" }}
    >
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
                  <Stack direction="row" spacing={1} alignItems="center" component="span" sx={{ flexWrap: "wrap" }}>
                    <Typography component="span" sx={{ fontWeight: 700 }}>{it.t}</Typography>
                    {it.tag && (
                      <Chip component="span" size="small" label={it.tag} dir="ltr"
                            sx={{ height: 20, fontFamily: "monospace" }} />
                    )}
                  </Stack>
                }
                secondary={it.d}
                secondaryTypographyProps={{ sx: { lineHeight: 1.9, mt: 0.25 } }}
              />
            </ListItem>
          ))}
        </List>
      </AccordionDetails>
    </Accordion>
  );
}
