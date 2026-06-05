import { useState, useMemo } from "react";
import {
  Box, Button, Typography, TextField, Switch, FormControlLabel, Stack, Divider,
  Collapse, Tabs, Tab, Paper, MenuItem, Chip, Alert,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import useMediaQuery from "@mui/material/useMediaQuery";
import SmartToyIcon from "@mui/icons-material/SmartToy";
import PaymentsIcon from "@mui/icons-material/Payments";
import SellIcon from "@mui/icons-material/Sell";
import ScheduleIcon from "@mui/icons-material/Schedule";
import NotificationsActiveIcon from "@mui/icons-material/NotificationsActive";
import PersonIcon from "@mui/icons-material/Person";
import DnsIcon from "@mui/icons-material/Dns";
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutline";
import TuneRoundedIcon from "@mui/icons-material/TuneRounded";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listSettings, updateSettings, refreshRate } from "../api/client";
import { useToast, errMsg } from "../components/Toast";

// A curated, hand-authored settings UI. Each field carries its own label/help/visibility,
// so the page is intentionally structured (not an auto-dump of every DB key). Internal-only
// settings (setup_done, owner_chat_id) are never rendered as editable fields.
type Getter = (key: string) => any;
type Field = {
  key: string;
  label: string;
  help?: string;
  type?: "text" | "number" | "bool" | "csv" | "multiline" | "select";
  advanced?: boolean;
  dir?: "ltr" | "rtl";
  min?: number;
  max?: number;
  options?: { value: string; label: string }[];
  when?: (v: Getter) => boolean;
};
type Sub = { title?: string; fields: Field[] };
type Section = {
  id: string;
  title: string;
  icon: JSX.Element;
  description?: string;
  note?: string;
  subs: Sub[];
};

// Settings that are machine-managed, not user-editable — hidden from the panel entirely.
const HIDDEN = new Set(["setup_done", "owner_chat_id", "toman_per_usdt_auto", "toman_per_usdt_auto_at"]);

const SECTIONS: Section[] = [
  {
    id: "telegram",
    title: "تلگرام و عضویت",
    icon: <SmartToyIcon fontSize="small" />,
    description:
      "ربات تلگرام، کانال/گروه عضویت اجباری، و پاک‌سازی اعضای غیرنماینده. برای ثبت کانال/گروه (حتی خصوصی) کافی است ربات را در آن ادمین کنید و یک پیام از همان‌جا را برای ربات فوروارد کنید تا شناسه‌اش خودکار پر شود.",
    subs: [
      {
        fields: [
          { key: "telegram_bot_token", label: "توکن ربات", help: "از @BotFather بگیرید.", dir: "ltr" },
        ],
      },
      {
        title: "کانال اطلاع‌رسانی",
        fields: [
          { key: "announcement_channel_id", label: "شناسه کانال", help: "با فوروارد یک پیام از کانال خودکار پر می‌شود.", dir: "ltr" },
          { key: "channel_membership_required", label: "الزام عضویت در کانال", help: "اگر روشن باشد، کاربر تا عضو کانال نشود نمی‌تواند از ربات استفاده کند." },
          { key: "announcement_channel_link", label: "لینک ثابت کانال", help: "اختیاری — برای کانال خصوصی لازم نیست؛ ربات خودش لینک عضویت یک‌بارمصرف می‌سازد.", advanced: true, dir: "ltr" },
        ],
      },
      {
        title: "گروه (اختیاری)",
        fields: [
          { key: "announcement_group_id", label: "شناسه گروه", help: "اگر می‌خواهید علاوه بر کانال، عضویت در یک گروه هم اجباری باشد.", dir: "ltr" },
          { key: "group_membership_required", label: "الزام عضویت در گروه", help: "اگر هم این و هم کانال روشن باشد، کاربر باید عضو هر دو باشد." },
          { key: "announcement_group_link", label: "لینک ثابت گروه", help: "اختیاری — برای گروه خصوصی لازم نیست.", advanced: true, dir: "ltr" },
        ],
      },
      {
        title: "پاک‌سازی و امنیت",
        fields: [
          { key: "channel_kick_enabled", label: "پاک‌سازی واقعی اعضای غیرنماینده", help: "خاموش = فقط گزارش آزمایشی. روشن = کسانی که نمایندهٔ ثبت‌شده نیستند از کانال/گروه حذف می‌شوند." },
          { key: "one_time_invite_links", label: "لینک عضویت یک‌بارمصرف", help: "برای هر کاربر یک لینک اختصاصی ساخته می‌شود." },
          { key: "kick_grace_minutes", label: "مهلت ارفاق پاک‌سازی (دقیقه)", help: "تازه‌واردها تا این مدت فرصت ثبت لینک دارند و حذف نمی‌شوند.", type: "number", min: 0, max: 1440, advanced: true },
        ],
      },
    ],
  },
  {
    id: "payments",
    title: "روش‌های پرداخت",
    icon: <PaymentsIcon fontSize="small" />,
    description:
      "روش‌هایی که روی فاکتور و در «پرداخت» ربات به نماینده نشان داده می‌شود. روشی که اطلاعاتش خالی باشد (آدرس کیف پول یا شماره کارت) نمایش داده نمی‌شود، حتی اگر روشن باشد.",
    subs: [
      {
        title: "روش‌های فعال",
        fields: [
          { key: "pay_usdt_enabled", label: "USDT (کیف پول + شناسهٔ تراکنش)" },
          { key: "pay_screenshot_enabled", label: "ارسال تصویر رسید" },
          { key: "pay_card_enabled", label: "کارت‌به‌کارت" },
        ],
      },
      {
        title: "اطلاعات USDT (BEP-20)",
        fields: [
          { key: "usdt_bep20_address", label: "آدرس کیف پول USDT", help: "آدرس مقصد روی شبکهٔ BEP-20.", dir: "ltr", when: (v) => !!v("pay_usdt_enabled") },
          { key: "usdt_bep20_contract", label: "قرارداد توکن USDT", advanced: true, dir: "ltr", when: (v) => !!v("pay_usdt_enabled") },
        ],
      },
      {
        title: "اطلاعات کارت بانکی",
        fields: [
          { key: "card_number", label: "شماره کارت", dir: "ltr", when: (v) => !!v("pay_card_enabled") },
          { key: "card_holder_name", label: "نام صاحب کارت", when: (v) => !!v("pay_card_enabled") },
        ],
      },
      {
        title: "تأیید تراکنش روی زنجیره (BscScan)",
        fields: [
          { key: "bscscan_api_key", label: "کلید API بی‌اسکن", advanced: true, dir: "ltr" },
          { key: "bscscan_api_url", label: "آدرس API بی‌اسکن", advanced: true, dir: "ltr" },
          { key: "min_confirmations", label: "حداقل تعداد تأیید", type: "number", min: 1, max: 100, advanced: true },
          { key: "payment_amount_tolerance_usdt", label: "اغماض مبلغ (USDT)", help: "اختلاف مجاز بین مبلغ فاکتور و واریزی.", type: "number", advanced: true },
          { key: "usdt_master_xpub", label: "xpub کیف پول مادر (HD)", advanced: true, dir: "ltr" },
        ],
      },
    ],
  },
  {
    id: "pricing",
    title: "قیمت‌گذاری",
    icon: <SellIcon fontSize="small" />,
    description: "قیمت پایهٔ فروش، نرخ تبدیل به USDT، و قواعد صورتحساب.",
    subs: [
      {
        fields: [
          { key: "default_price_per_gb", label: "قیمت پیش‌فرض هر گیگ (تومان)", help: "اگر برای نماینده‌ای قیمت اختصاصی ثبت نشده باشد، این اعمال می‌شود.", type: "number", min: 0 },
          { key: "rate_mode", label: "حالت نرخ تبدیل تومان→USDT", type: "select",
            help: "«خودکار» نرخ تتر به تومان را آنلاین می‌خواند (تترلند/والکس)؛ «دستی» از نرخ پایین استفاده می‌کند.",
            options: [{ value: "manual", label: "دستی" }, { value: "auto", label: "خودکار (آنلاین)" }] },
          { key: "toman_per_usdt", label: "نرخ تبدیل دستی (تومان به ازای هر USDT)", type: "number", min: 0,
            help: "در حالت «دستی» این نرخ استفاده می‌شود؛ در حالت «خودکار» اگر دریافت آنلاین ناموفق بود، همین مقدار جایگزین می‌شود." },
          { key: "free_under_gb", label: "آستانهٔ کانفیگ رایگان (گیگ)", help: "کانفیگ‌هایی با حجم کوچک‌تر یا مساوی این مقدار، تستی و رایگان حساب می‌شوند (مثلاً ۱ → هم ۰٫۵ و هم ۱ گیگ رایگان، ۱٫۵ به بالا محاسبه می‌شود).", type: "number", min: 0 },
          { key: "min_sale_toman", label: "حداقل فروش هر نماینده (تومان)", help: "۰ = غیرفعال. اگر مبلغ فاکتور از این کمتر شد، همین مبلغ لحاظ می‌شود.", type: "number", min: 0 },
          { key: "metering_enabled", label: "متر مصرف ضد سوءاستفاده", help: "محاسبهٔ مصرف فراتر از سهمیه (ترفند ریست روزانه) و تمدید با ویرایش." },
          { key: "excluded_usage_gb", label: "حجم‌های معاف اضافی (گیگ، با کاما)", help: "اندازه‌های دقیقی که نباید محاسبه شوند، جدا با کاما.", type: "csv", advanced: true },
        ],
      },
    ],
  },
  {
    id: "schedule",
    title: "زمان‌بندی کارهای خودکار",
    icon: <ScheduleIcon fontSize="small" />,
    note:
      "همهٔ ساعت‌ها به وقت ایران است. کارهای «هر چند ساعت/دقیقه» روی ساعت‌های رُند اجرا می‌شوند (مثلاً هر ۲ ساعت = ۰۰، ۰۲، …) و به زمان دیپلوی وابسته نیستند. برای فاصلهٔ کاملاً یکنواخت عددی بگذارید که ۲۴ (ساعت) یا ۶۰ (دقیقه) بر آن بخش‌پذیر باشد: ۱، ۲، ۳، ۴، ۶، ۸، ۱۲. تغییرات بلافاصله و بدون ری‌استارت اعمال می‌شوند.",
    subs: [
      {
        title: "پشتیبان‌گیری و همگام‌سازی",
        fields: [
          { key: "backup_enabled", label: "پشتیبان‌گیری خودکار به تلگرام" },
          { key: "backup_interval_hours", label: "پشتیبان‌گیری: هر چند ساعت", type: "number", min: 1, max: 23, when: (v) => !!v("backup_enabled") },
          { key: "sync_interval_hours", label: "همگام‌سازی پنل‌ها: هر چند ساعت", type: "number", min: 1, max: 23 },
          { key: "guard_interval_minutes", label: "گارد کانال/گروه: هر چند دقیقه", type: "number", min: 1, max: 59 },
        ],
      },
      {
        title: "فاکتور و یادآوری",
        fields: [
          { key: "invoice_day_of_month", label: "صدور فاکتور ماهانه: روز ماه", type: "number", min: 1, max: 28 },
          { key: "invoice_hour", label: "صدور فاکتور ماهانه: ساعت", type: "number", min: 0, max: 23 },
          { key: "dunning_hour", label: "اجرای یادآوری/مسدودسازی روزانه: ساعت", type: "number", min: 0, max: 23 },
        ],
      },
    ],
  },
  {
    id: "dunning",
    title: "یادآوری و مسدودسازی",
    icon: <NotificationsActiveIcon fontSize="small" />,
    description:
      "روزشماری از زمان ارسال فاکتور تا یادآوری‌ها و مسدودسازی. مسدودسازی واقعی به‌طور پیش‌فرض خاموش (آزمایشی) است تا تا وقتی مطمئن نشده‌اید کاربری مسدود نشود.",
    subs: [
      {
        fields: [
          { key: "reminder1_day", label: "یادآوری اول (روز پس از صدور)", type: "number", min: 0, max: 60 },
          { key: "reminder2_day", label: "یادآوری دوم (روز)", type: "number", min: 0, max: 60 },
          { key: "warning_day", label: "اخطار نهایی (روز)", type: "number", min: 0, max: 60 },
          { key: "enforcement_day", label: "مسدودسازی (روز)", type: "number", min: 0, max: 60 },
          { key: "enforcement_enabled", label: "مسدودسازی واقعی", help: "خاموش = فقط گزارش آزمایشی. روشن = کاربران نمایندهٔ بدهکار و زیرمجموعه‌هایش غیرفعال می‌شوند." },
          { key: "auto_restore_on_payment", label: "بازگردانی خودکار با پرداخت", help: "پس از تأیید پرداخت، نماینده و کاربرانش خودکار به حالت قبل برمی‌گردند." },
        ],
      },
    ],
  },
  {
    id: "general",
    title: "عمومی",
    icon: <PersonIcon fontSize="small" />,
    description: "اطلاعات مالک سامانه.",
    subs: [
      {
        fields: [
          { key: "owner_name", label: "نام مالک" },
          { key: "owner_telegram", label: "آیدی تلگرام مالک", help: "مثلاً @username — برای نمایش/پشتیبانی.", dir: "ltr" },
        ],
      },
    ],
  },
  {
    id: "deploy",
    title: "دامنه و HTTPS",
    icon: <DnsIcon fontSize="small" />,
    note:
      "این مقادیر هنگام نصب روی سرور استفاده می‌شوند: دامنه را وارد کنید، رکورد A آن را به IP سرور بدهید، و نصب‌کننده خودکار گواهی SSL را می‌گیرد و تمدید می‌کند.",
    subs: [
      {
        fields: [
          { key: "server_domain", label: "دامنهٔ سرور", dir: "ltr" },
          { key: "https_enabled", label: "فعال‌سازی HTTPS خودکار" },
          { key: "acme_email", label: "ایمیل برای گواهی SSL", dir: "ltr", when: (v) => !!v("https_enabled") },
        ],
      },
    ],
  },
  {
    id: "templates",
    title: "متن پیام‌ها",
    icon: <ChatBubbleOutlineIcon fontSize="small" />,
    description: "متن پیام‌های ربات. از placeholderها مثل {name}، {period}، {amount_usdt} و {payment_instructions} استفاده کنید.",
    subs: [
      {
        fields: [
          { key: "tpl_welcome", label: "پیام خوش‌آمد", type: "multiline" },
          { key: "tpl_membership", label: "پیام نیاز به عضویت", type: "multiline" },
          { key: "tpl_menu", label: "پیام منو", type: "multiline" },
          { key: "tpl_link_matched", label: "ثبت موفق لینک", type: "multiline" },
          { key: "tpl_link_not_found", label: "لینک نامعتبر", type: "multiline" },
          { key: "tpl_invoice", label: "متن فاکتور", type: "multiline" },
          { key: "tpl_reminder1", label: "یادآوری اول", type: "multiline" },
          { key: "tpl_reminder2", label: "یادآوری دوم", type: "multiline" },
          { key: "tpl_warning", label: "اخطار نهایی", type: "multiline" },
          { key: "tpl_payment_received", label: "تأیید پرداخت", type: "multiline" },
          { key: "tpl_payment_rejected", label: "رد پرداخت", type: "multiline" },
        ],
      },
    ],
  },
];

export default function Settings() {
  const qc = useQueryClient();
  const theme = useTheme();
  const compact = useMediaQuery(theme.breakpoints.down("md"));
  const { node, show } = useToast();
  const { data = [] } = useQuery({ queryKey: ["settings"], queryFn: listSettings });
  const [edits, setEdits] = useState<Record<string, any>>({});
  const [active, setActive] = useState(0);
  const [showAdvanced, setShowAdvanced] = useState<Record<string, boolean>>({});

  const byKey = useMemo(() => {
    const m: Record<string, any> = {};
    for (const s of data) m[s.key] = s;
    return m;
  }, [data]);

  const setVal = (key: string, value: any) => setEdits((e) => ({ ...e, [key]: value }));
  const getVal: Getter = (key) => (key in edits ? edits[key] : byKey[key]?.value);
  const dirtyCount = Object.keys(edits).length;

  // Any non-hidden setting not covered by a curated field falls into a "متفرقه" section,
  // so a newly-added backend setting is never silently lost from the panel.
  const sections = useMemo(() => {
    const covered = new Set<string>();
    SECTIONS.forEach((s) => s.subs.forEach((sub) => sub.fields.forEach((f) => covered.add(f.key))));
    const leftover = data
      .filter((s: any) => !covered.has(s.key) && !HIDDEN.has(s.key))
      .map((s: any) => ({ key: s.key, label: s.key }));
    if (!leftover.length) return SECTIONS;
    return [
      ...SECTIONS,
      { id: "misc", title: "متفرقه", icon: <TuneRoundedIcon fontSize="small" />, subs: [{ fields: leftover }] } as Section,
    ];
  }, [data]);

  const save = useMutation({
    mutationFn: () => {
      const items = Object.entries(edits).map(([key, value]) => {
        if (byKey[key] && Array.isArray(byKey[key].value) && typeof value === "string")
          value = value.split(",").map((x) => parseFloat(x.trim())).filter((n) => !isNaN(n));
        return { key, value };
      });
      return updateSettings(items);
    },
    onSuccess: () => { show("تنظیمات ذخیره شد"); setEdits({}); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const refreshRateM = useMutation({
    mutationFn: refreshRate,
    onSuccess: (r: any) => { show(`نرخ آنلاین به‌روزرسانی شد: ${Number(r?.rate || 0).toLocaleString("en-US")} تومان`); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const renderField = (f: Field) => {
    const meta = byKey[f.key];
    if (!meta) return null;
    if (f.when && !f.when(getVal)) return null;
    const v = getVal(f.key);
    const isSecret = !!meta.is_secret;
    const type = f.type || (typeof meta.value === "boolean" ? "bool" : typeof meta.value === "number" ? "number" : "text");

    if (type === "bool")
      return (
        <FormControlLabel key={f.key} sx={{ alignItems: "center" }}
          control={<Switch checked={!!v} onChange={(e) => setVal(f.key, e.target.checked)} />}
          label={
            <Box>
              <Typography variant="body2">{f.label}</Typography>
              {f.help && <Typography variant="caption" color="text.secondary">{f.help}</Typography>}
            </Box>
          }
        />
      );
    if (type === "select")
      return (
        <TextField key={f.key} select label={f.label} value={v ?? ""} fullWidth size="small"
          helperText={f.help} onChange={(e) => setVal(f.key, e.target.value)}>
          {(f.options || []).map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
        </TextField>
      );
    if (type === "multiline")
      return <TextField key={f.key} label={f.label} value={v ?? ""} multiline minRows={2} fullWidth size="small"
        helperText={f.help} onChange={(e) => setVal(f.key, e.target.value)} />;
    if (type === "csv")
      return <TextField key={f.key} label={f.label} value={Array.isArray(v) ? v.join(", ") : v ?? ""} fullWidth size="small"
        helperText={f.help} onChange={(e) => setVal(f.key, e.target.value)} />;
    if (isSecret)
      return <TextField key={f.key} label={f.label} placeholder={meta.has_value ? "•••• (برای تغییر وارد کنید)" : "تنظیم نشده"}
        inputProps={{ dir: "ltr" }} value={f.key in edits ? edits[f.key] : ""} fullWidth size="small"
        helperText={f.help} onChange={(e) => setVal(f.key, e.target.value)} />;
    if (type === "number") {
      const bounded = f.min !== undefined || f.max !== undefined;
      const range = bounded ? `مجاز: ${f.min ?? "?"} تا ${f.max ?? "?"}` : "";
      return <TextField key={f.key} label={f.label} type="number" value={v ?? 0} fullWidth size="small"
        inputProps={bounded ? { min: f.min, max: f.max } : undefined}
        helperText={[f.help, range].filter(Boolean).join(" — ") || undefined}
        onChange={(e) => setVal(f.key, Number(e.target.value))} />;
    }
    return <TextField key={f.key} label={f.label} value={v ?? ""} fullWidth size="small"
      inputProps={f.dir === "ltr" ? { dir: "ltr" } : undefined}
      helperText={f.help} onChange={(e) => setVal(f.key, e.target.value)} />;
  };

  const renderSection = (sec: Section) => {
    const advanced: Field[] = [];
    const subs = sec.subs.map((sub) => {
      const normal = sub.fields.filter((f) => !f.advanced && (!f.when || f.when(getVal)));
      sub.fields.filter((f) => f.advanced && (!f.when || f.when(getVal))).forEach((f) => advanced.push(f));
      return { title: sub.title, fields: normal };
    }).filter((sub) => sub.fields.length);
    const advOpen = !!showAdvanced[sec.id];

    return (
      <Paper variant="outlined" sx={{ p: { xs: 2, sm: 3 } }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
          {sec.icon}
          <Typography variant="h6" sx={{ fontWeight: 700 }}>{sec.title}</Typography>
        </Stack>
        {sec.description && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{sec.description}</Typography>
        )}
        {sec.note && (
          <Alert severity="info" icon={<InfoOutlinedIcon fontSize="inherit" />} sx={{ mb: 2 }}>
            {sec.note}
          </Alert>
        )}

        {/* Owner-connection status — read-only, not an editable field. */}
        {sec.id === "general" && (
          <Box sx={{ mb: 2 }}>
            {byKey["owner_chat_id"]?.value
              ? <Chip color="success" size="small" icon={<CheckCircleIcon />} label="تلگرام مالک متصل است — پشتیبان و هشدارها ارسال می‌شوند" />
              : <Chip color="warning" size="small" icon={<InfoOutlinedIcon />} label="هنوز در ربات /start نزده‌اید؛ تا متصل نشوید پشتیبان خودکار ارسال نمی‌شود" />}
          </Box>
        )}

        {/* Live USDT→Toman rate status + manual refresh — read-only display. */}
        {sec.id === "pricing" && (
          <Box sx={{ mb: 2 }}>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              {Number(byKey["toman_per_usdt_auto"]?.value) > 0
                ? <Chip color={getVal("rate_mode") === "auto" ? "success" : "default"} size="small"
                    label={`نرخ آنلاین تتر: ${Number(byKey["toman_per_usdt_auto"].value).toLocaleString("en-US")} تومان`} />
                : <Chip color="warning" size="small" icon={<InfoOutlinedIcon />} label="نرخ آنلاین هنوز دریافت نشده" />}
              {byKey["toman_per_usdt_auto_at"]?.value && (
                <Typography variant="caption" color="text.secondary" dir="ltr">
                  {String(byKey["toman_per_usdt_auto_at"].value).replace("T", " ").slice(0, 16)} UTC
                </Typography>
              )}
              <Button size="small" variant="outlined" disabled={refreshRateM.isPending} onClick={() => refreshRateM.mutate()}>
                به‌روزرسانی نرخ
              </Button>
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
              در حالت «خودکار»، فاکتورها با همین نرخِ آنلاین به USDT تبدیل می‌شوند و هر ساعت به‌روز می‌شود.
            </Typography>
          </Box>
        )}

        <Stack spacing={3}>
          {subs.map((sub, i) => (
            <Box key={i}>
              {sub.title && (
                <Divider textAlign="right" sx={{ mb: 1.5 }}>
                  <Typography variant="overline" color="text.secondary">{sub.title}</Typography>
                </Divider>
              )}
              <Stack spacing={2}>{sub.fields.map(renderField)}</Stack>
            </Box>
          ))}
        </Stack>

        {advanced.length > 0 && (
          <Box sx={{ mt: 3 }}>
            <Button size="small" color="inherit" startIcon={<TuneRoundedIcon />}
              endIcon={<ExpandMoreIcon sx={{ transform: advOpen ? "rotate(180deg)" : "none", transition: "0.2s" }} />}
              onClick={() => setShowAdvanced((s) => ({ ...s, [sec.id]: !advOpen }))}>
              تنظیمات پیشرفته ({advanced.length})
            </Button>
            <Collapse in={advOpen}>
              <Stack spacing={2} sx={{ mt: 2, pt: 2, borderTop: 1, borderColor: "divider" }}>
                {advanced.map(renderField)}
              </Stack>
            </Collapse>
          </Box>
        )}
      </Paper>
    );
  };

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center"
        sx={{ position: "sticky", top: 0, zIndex: 3, bgcolor: "background.default", py: 1.5, mb: 2 }}>
        <Box>
          <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>تنظیمات سامانه</Typography>
          <Typography variant="caption" color="text.secondary">
            پیکربندی ربات، پرداخت، قیمت و زمان‌بندی — مقادیر حساس رمزنگاری می‌شوند.
          </Typography>
        </Box>
        <Button variant="contained" onClick={() => save.mutate()} disabled={save.isPending || dirtyCount === 0}>
          ذخیره تغییرات{dirtyCount ? ` (${dirtyCount})` : ""}
        </Button>
      </Stack>

      <Box sx={{ display: "flex", flexDirection: compact ? "column" : "row", gap: 2, alignItems: "flex-start" }}>
        <Paper variant="outlined" sx={{ flexShrink: 0, width: compact ? "100%" : 240, position: compact ? "static" : "sticky", top: 80, overflow: "hidden" }}>
          <Tabs
            orientation={compact ? "horizontal" : "vertical"}
            variant={compact ? "scrollable" : "standard"}
            scrollButtons="auto"
            value={active}
            onChange={(_, v) => setActive(v)}
            sx={{ ".MuiTab-root": { justifyContent: "flex-start", minHeight: 48, alignItems: "center" } }}
          >
            {sections.map((s) => (
              <Tab key={s.id} icon={s.icon} iconPosition="start" label={s.title} sx={{ textAlign: "right" }} />
            ))}
          </Tabs>
        </Paper>

        <Box sx={{ flex: 1, minWidth: 0, width: "100%" }}>
          {sections[active] && renderSection(sections[active])}
        </Box>
      </Box>
      {node}
    </Box>
  );
}
