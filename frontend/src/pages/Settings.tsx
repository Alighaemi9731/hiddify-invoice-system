import { useState, useMemo } from "react";
import {
  Box, Button, Typography, TextField, Switch, FormControlLabel, Stack, Divider,
  Collapse, Tabs, Tab, Paper, MenuItem, Chip, Alert,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import useMediaQuery from "@mui/material/useMediaQuery";
import { TIER1_BLUR, TIER2_BG, TIER2_BLUR } from "../themeTokens";
import { fmtDateTime } from "../format";
import { NumberField, numberValue } from "../components/NumberField";
import SmartToyIcon from "@mui/icons-material/esm/SmartToy";
import PaymentsIcon from "@mui/icons-material/esm/Payments";
import SellIcon from "@mui/icons-material/esm/Sell";
import ScheduleIcon from "@mui/icons-material/esm/Schedule";
import NotificationsActiveIcon from "@mui/icons-material/esm/NotificationsActive";
import PersonIcon from "@mui/icons-material/esm/Person";
import DnsIcon from "@mui/icons-material/esm/Dns";
import ChatBubbleOutlineIcon from "@mui/icons-material/esm/ChatBubbleOutline";
import TuneRoundedIcon from "@mui/icons-material/esm/TuneRounded";
import StorefrontIcon from "@mui/icons-material/esm/Storefront";
import TrackChangesIcon from "@mui/icons-material/esm/TrackChanges";
import ScienceIcon from "@mui/icons-material/esm/Science";
import ExpandMoreIcon from "@mui/icons-material/esm/ExpandMore";
import CheckCircleIcon from "@mui/icons-material/esm/CheckCircle";
import InfoOutlinedIcon from "@mui/icons-material/esm/InfoOutlined";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listSettings, updateSettings, refreshRate, listPanels } from "../api/client";
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
  // A select whose choices are rows from another table (fetched at render time) rather than a fixed
  // list. `numeric` sends the picked value back as a number — the API validates a setting against
  // the type of its default, so an id-valued select must not save the option's string.
  optionsFrom?: "panels";
  numeric?: boolean;
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

// Pick an editor for an uncurated setting from the value the API returns, so the «متفرقه»
// fallback still gets a switch for a boolean and a numeric input for a number instead of
// turning every value into a free-text string on save.
function inferFieldType(value: unknown): Field["type"] {
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "number";
  if (Array.isArray(value)) return "csv";
  if (typeof value === "string" && value.length > 80) return "multiline";
  return "text";
}

// Settings that are machine-managed, not user-editable — hidden from the panel entirely.
const HIDDEN = new Set(["setup_done", "owner_chat_id", "toman_per_usdt_auto", "toman_per_usdt_auto_at", "ton_toman_auto", "avax_toman_auto", "last_backup_at", "scheduler_last_heartbeat", "error_digest_last_ts"]);

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
          { key: "announcement_channel_id", label: "شناسه کانال", help: "با فوروارد یک پیام از کانال خودکار پر می‌شود — یا همین‌جا مستقیم ویرایش کنید؛ تغییر بلافاصله اعمال می‌شود.", dir: "ltr" },
          { key: "channel_membership_required", label: "الزام عضویت در کانال", help: "اگر روشن باشد، کاربر تا عضو کانال نشود نمی‌تواند از ربات استفاده کند." },
          { key: "announcement_channel_link", label: "لینک ثابت کانال", help: "اختیاری — برای کانال خصوصی لازم نیست؛ ربات خودش لینک عضویت یک‌بارمصرف می‌سازد.", advanced: true, dir: "ltr" },
        ],
      },
      {
        title: "گروه (اختیاری)",
        fields: [
          { key: "announcement_group_id", label: "شناسه گروه", help: "اگر می‌خواهید علاوه بر کانال، عضویت در یک گروه هم اجباری باشد. با فوروارد پیام از گروه پر می‌شود یا همین‌جا مستقیم ویرایش کنید؛ تغییر بلافاصله اعمال می‌شود.", dir: "ltr" },
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
          { key: "pay_ton_enabled", label: "گرام (GRAM)" },
          { key: "pay_avax_enabled", label: "اوالانچ (AVAX)" },
        ],
      },
      {
        title: "اطلاعات USDT (BEP-20)",
        fields: [
          { key: "usdt_bep20_address", label: "آدرس کیف پول USDT", help: "آدرس مقصد روی شبکهٔ BEP-20.", dir: "ltr", when: (v) => !!v("pay_usdt_enabled") },
          { key: "usdt_bep20_contract", label: "قرارداد توکن USDT", advanced: true, dir: "ltr", when: (v) => !!v("pay_usdt_enabled") },
          { key: "bsc_rpc_url", label: "نودِ RPC شبکهٔ BSC", advanced: true, dir: "ltr",
            help: "برای خواندنِ واریزیِ USDT از زنجیره هنگام تأییدِ دستی (رایگان، بدون کلید). پیش‌فرض را تغییر ندهید مگر به محدودیتِ نرخ بخورید.", when: (v) => !!v("pay_usdt_enabled") },
        ],
      },
      {
        title: "اطلاعات گرام (GRAM) — شبکهٔ TON",
        fields: [
          { key: "ton_wallet_address", label: "آدرس کیف پول گرام (شبکهٔ TON)", help: "آدرس مقصدِ گرام (GRAM) روی شبکهٔ TON. مبلغِ معادلِ GRAM به‌صورت آنلاین (والکس) محاسبه و به مشتری نشان داده می‌شود؛ تأیید به‌صورت دستی (با تصویر رسید) انجام می‌شود.", dir: "ltr", when: (v) => !!v("pay_ton_enabled") },
          { key: "ton_amount_tolerance_pct", label: "اغماضِ مبلغِ گرام (درصد)", type: "number", min: 0, max: 100,
            help: "هنگام تأییدِ دستیِ پرداختِ گرام، مبلغِ واقعیِ واریزشده از زنجیرهٔ TON خوانده و با مبلغِ فاکتور مقایسه می‌شود؛ اختلافِ تا این درصد «مطابق» در نظر گرفته می‌شود. پیش‌فرض ۵.", when: (v) => !!v("pay_ton_enabled") },
          { key: "toncenter_api_key", label: "کلید API تون‌سنتر (اختیاری)", advanced: true, dir: "ltr",
            help: "برای خواندنِ واریزیِ گرام از toncenter (شبکهٔ TON)؛ بدون کلید هم کار می‌کند ولی با محدودیتِ نرخ. خالی بگذارید مگر به سقفِ درخواست بخورید.", when: (v) => !!v("pay_ton_enabled") },
        ],
      },
      {
        title: "اطلاعات اوالانچ (AVAX)",
        fields: [
          { key: "avax_address", label: "آدرس کیف پول اوالانچ (AVAX)", help: "آدرس مقصدِ AVAX روی شبکهٔ Avalanche C-Chain. مبلغِ معادلِ AVAX به‌صورت آنلاین (CoinGecko × نرخ تتر) محاسبه و به مشتری نشان داده می‌شود؛ تأیید به‌صورت دستی (لینکِ Snowtrace) انجام می‌شود.", dir: "ltr", when: (v) => !!v("pay_avax_enabled") },
          { key: "avax_amount_tolerance_pct", label: "اغماضِ مبلغِ اوالانچ (درصد)", type: "number", min: 0, max: 100,
            help: "هنگام «بررسی واریزی روی زنجیره»، مبلغِ واقعیِ واریزشده از زنجیرهٔ Avalanche خوانده و با مبلغِ فاکتور مقایسه می‌شود؛ اختلافِ تا این درصد «مطابق» در نظر گرفته می‌شود. پیش‌فرض ۵.", when: (v) => !!v("pay_avax_enabled") },
          { key: "avalanche_rpc_url", label: "نودِ RPC شبکهٔ Avalanche", advanced: true, dir: "ltr",
            help: "برای خواندنِ واریزیِ AVAX از زنجیره هنگام تأییدِ دستی (رایگان، بدون کلید). پیش‌فرض را تغییر ندهید مگر به محدودیتِ نرخ بخورید.", when: (v) => !!v("pay_avax_enabled") },
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
        title: "تأیید خودکار پرداخت روی زنجیره",
        fields: [
          { key: "payment_auto_confirm_enabled", label: "تأیید خودکار واریزیِ مطابق فاکتور",
            help: "وقتی نماینده شناسهٔ تراکنش (USDT / گرام / اوالانچ) را ثبت می‌کند، واریزی از زنجیره خوانده می‌شود؛ اگر مقصد، مبلغ و تعداد تأییدها دقیقاً مطابق فاکتور بود، سامانه همان‌جا آن را تأیید و فاکتور را تسویه می‌کند. هر مورد دیگری — مبلغِ متفاوت، تأییدِ کم، تراکنشِ قدیمی، یا نخواندنِ زنجیره — مثل قبل در انتظارِ تأیید دستیِ شما می‌ماند. رسیدِ تصویری و کارت‌به‌کارت هرگز خودکار تأیید نمی‌شوند." },
          { key: "payment_auto_confirm_max_age_hours", label: "حداکثر سنِ تراکنش برای تأیید خودکار (ساعت)", type: "number", min: 1, max: 8760,
            help: "فقط واریزی‌هایی که تا این تعداد ساعت قبل انجام شده‌اند خودکار تأیید می‌شوند. آدرسِ کیف پول شما عمومی است؛ این محدودیت جلوی ادعای شناسهٔ یک واریزِ قدیمیِ ثبت‌نشده را می‌گیرد. پیش‌فرض ۲۴.",
            when: (v) => !!v("payment_auto_confirm_enabled") },
        ],
      },
      {
        title: "تأیید تراکنش روی زنجیره (BscScan)",
        fields: [
          { key: "bscscan_api_key", label: "کلید API بی‌اسکن", advanced: true, dir: "ltr" },
          { key: "bscscan_api_url", label: "آدرس API بی‌اسکن", advanced: true, dir: "ltr" },
          { key: "min_confirmations", label: "حداقل تعداد تأیید", type: "number", min: 1, max: 100, advanced: true,
            help: "حداقل تأییدِ لازم روی شبکه (USDT و AVAX) برای اینکه واریزی خودکار تأیید شود. پیش‌فرض ۱۲؛ اگر پرداخت‌ها زیاد به تأیید دستی می‌افتند این عدد را کم کنید." },
          { key: "payment_amount_tolerance_usdt", label: "اغماض مبلغ (USDT)", help: "اختلاف مجاز بین مبلغ فاکتور و واریزی — همین عدد ملاکِ «مطابق بودن» در تأیید خودکار هم هست.", type: "number", advanced: true },
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
          { key: "default_price_per_gb", label: "قیمت پیش‌فرض هر گیگابایت (تومان)", help: "اگر برای نماینده‌ای قیمت اختصاصی ثبت نشده باشد، این اعمال می‌شود.", type: "number", min: 0 },
          { key: "rate_mode", label: "حالت نرخ تتر (USDT→تومان)", type: "select",
            help: "«خودکار» نرخ تتر را آنلاین می‌خواند؛ «دستی» از نرخ پایین استفاده می‌کند.",
            options: [{ value: "manual", label: "دستی" }, { value: "auto", label: "خودکار (آنلاین)" }] },
          { key: "rate_source", label: "منبعِ نرخِ آنلاین", type: "select",
            help: "نرخ تتر از کدام صرافی خوانده شود (منبعِ دیگر به‌عنوان فالبک). گرام فقط از والکس خوانده می‌شود.",
            options: [{ value: "wallex", label: "والکس" }, { value: "tetherland", label: "تترلند" }],
            when: (v) => v("rate_mode") === "auto" },
          { key: "toman_per_usdt", label: "نرخ تترِ دستی (تومان به ازای هر USDT)", type: "number", min: 0,
            help: "در حالت «دستی» این نرخ استفاده می‌شود؛ در حالت «خودکار» اگر دریافت آنلاین ناموفق بود، همین مقدار جایگزین می‌شود." },
          { key: "rate_max_age_hours", label: "کهنگی مجاز نرخ آنلاین (ساعت)", type: "number", min: 0, max: 8760, advanced: true,
            help: "در حالت «خودکار»، اگر آخرین نرخ آنلاین قدیمی‌تر از این مقدار باشد، موقع صدور فاکتور به نرخ دستی برمی‌گردد. ۰ = غیرفعال. پیش‌فرض ۴۸.", when: (v) => v("rate_mode") === "auto" },
          { key: "ton_rate_mode", label: "حالت نرخ گرام (GRAM→تومان)", type: "select",
            help: "مثل تتر: «خودکار» نرخ گرام را آنلاین از والکس (بازارِ GRAMTMN) می‌خواند؛ «دستی» از نرخ پایین استفاده می‌کند.",
            options: [{ value: "manual", label: "دستی" }, { value: "auto", label: "خودکار (آنلاین)" }],
            when: (v) => !!v("pay_ton_enabled") },
          { key: "ton_toman_manual", label: "نرخ گرامِ دستی (تومان به ازای هر GRAM)", type: "number", min: 0,
            help: "در حالت دستیِ گرام این نرخ استفاده می‌شود؛ در حالت خودکار به‌عنوان فالبک.",
            when: (v) => !!v("pay_ton_enabled") },
          { key: "avax_rate_mode", label: "حالت نرخ اوالانچ (AVAX→تومان)", type: "select",
            help: "«خودکار» نرخ AVAX را آنلاین می‌خواند (CoinGecko برای AVAX→دلار × نرخ تتر)؛ «دستی» از نرخ پایین استفاده می‌کند.",
            options: [{ value: "manual", label: "دستی" }, { value: "auto", label: "خودکار (آنلاین)" }],
            when: (v) => !!v("pay_avax_enabled") },
          { key: "avax_toman_manual", label: "نرخ اوالانچِ دستی (تومان به ازای هر AVAX)", type: "number", min: 0,
            help: "در حالت دستیِ AVAX این نرخ استفاده می‌شود؛ در حالت خودکار به‌عنوان فالبک.",
            when: (v) => !!v("pay_avax_enabled") },
          { key: "free_under_gb", label: "آستانهٔ کانفیگ رایگان (گیگابایت)", help: "کانفیگ‌هایی با حجم کوچک‌تر یا مساوی این مقدار، تستی و رایگان حساب می‌شوند (مثلاً ۱ → هم ۰٫۵ و هم ۱ گیگابایت رایگان، ۱٫۵ به بالا محاسبه می‌شود).", type: "number", min: 0 },
          { key: "min_sale_toman", label: "حداقل فروش هر نماینده (تومان)", help: "۰ = غیرفعال. حداقلِ مبلغِ فاکتور برای هر نماینده و مجموعِ زیرمجموعه‌هایش (روی هم، نه تک‌تک). اگر مجموعِ فروش کمتر از این شد، همین مبلغ به‌عنوان فاکتور صادر می‌شود؛ ولی PDF و مصرف دقیقِ واقعی می‌ماند و در متنِ فاکتور توضیح داده می‌شود. ماهِ اولِ هر نماینده معاف است و از ماهِ دوم اعمال می‌شود. برای یک نماینده می‌توانید از «ویرایش نماینده» مقدارِ اختصاصی بگذارید.", type: "number", min: 0 },
          { key: "metering_enabled", label: "متر مصرف ضد سوءاستفاده", help: "محاسبهٔ مصرف فراتر از سهمیه (ترفند ریست روزانه) و تمدید با ویرایش." },
          { key: "overage_tolerance_gb", label: "آستانهٔ اغماض مصرف اضافه (گیگابایت)", help: "اگر سرریزِ یک کاربر زیرِ این مقدار باشد، کلاً نادیده گرفته می‌شود (تأخیرِ قطعِ xray؛ چند صد مگابایت پس از پُر شدنِ حجم — سوءاستفاده نیست). اگر بالای این مقدار باشد، کلِ سرریز محاسبه می‌شود (مصرفِ اضافهٔ واقعی). ریستِ واقعی چندین گیگابایت است و همیشه محاسبه می‌شود. پیش‌فرض ۳", type: "number", min: 0, when: (v) => !!v("metering_enabled") },
          { key: "deleted_full_quota_over_gb", label: "سقفِ مصرفِ کاربرِ حذف‌شده برای فاکتورِ کاملِ حجم (گیگابایت)", type: "number", min: 0,
            help: "اگر نماینده کاربری را از پنل حذف کند: تا وقتی مصرفش زیرِ این مقدار باشد، فقط همان مصرف فاکتور می‌شود؛ اگر مصرفش به این مقدار یا بیشتر رسیده باشد، کلِّ حجمِ فروخته‌شده (مثلاً ۵۰ گیگابایت) فاکتور می‌شود — تا نماینده با حذفِ کانفیگ فقط بابتِ بخشِ مصرف‌شده پول ندهد. ۰ = غیرفعال (فقط مصرف). پیش‌فرض ۵." },
          { key: "excluded_usage_gb", label: "حجم‌های معاف اضافی (گیگابایت، با کاما)", help: "اندازه‌های دقیقی که نباید محاسبه شوند، جدا با کاما.", type: "csv", advanced: true },
          { key: "storefront_monthly_fee_toman", label: "اجارهٔ ماهانهٔ ربات فروشگاهی (تومان)", type: "number", min: 0,
            help: "مبلغی که هر ماه بابتِ داشتنِ ربات فروشگاهی به فاکتورِ نماینده اضافه می‌شود. فقط از نماینده‌ای گرفته می‌شود که ربات فروشگاهی‌اش واقعاً فعال است. ۰ = رایگان. برای هر نماینده جداگانه هم قابل تنظیم است («ویرایش نماینده»)." },
          { key: "billing_max_snapshot_age_hours", label: "کهنگی مجازِ همگام‌سازی برای صدور فاکتور (ساعت)", type: "number", min: 0, max: 8760, advanced: true,
            help: "اگر آخرین همگام‌سازیِ موفقِ یک پنل از این مقدار قدیمی‌تر باشد، آن پنل در صدور فاکتور نادیده گرفته می‌شود تا با دادهٔ کهنه فاکتور صادر نشود. ۰ = بدون محدودیت. پیش‌فرض ۲۶." },
          { key: "high_volume_gb_threshold", label: "آستانهٔ «کاربران پرمصرف» (گیگابایت)", type: "number", min: 1, advanced: true,
            help: "فقط برای گزارش: در فهرستِ «کاربران پرمصرف»، کاربرانی با حجمِ بیش از این مقدار نشان داده می‌شوند. روی فاکتور هیچ اثری ندارد. پیش‌فرض ۱۰۰۰." },
        ],
      },
    ],
  },
  {
    id: "schedule",
    title: "زمان‌بندی کارهای خودکار",
    icon: <ScheduleIcon fontSize="small" />,
    note:
      "همهٔ ساعت‌ها به وقت ایران است. کارهای «هر چند ساعت/دقیقه» با فاصلهٔ واقعی و ثابت اجرا می‌شوند و راه‌اندازی مجدد یا استقرار سامانه، شمارش آن‌ها را از نو آغاز نمی‌کند. تغییرات بلافاصله و بدون راه‌اندازی مجدد اعمال می‌شوند.",
    subs: [
      {
        title: "پشتیبان‌گیری و همگام‌سازی",
        fields: [
          { key: "backup_enabled", label: "پشتیبان‌گیری خودکار به تلگرام" },
          { key: "backup_interval_hours", label: "پشتیبان‌گیری: هر چند ساعت", type: "number", min: 1, max: 24, when: (v) => !!v("backup_enabled") },
          { key: "backup_passphrase", label: "گذرواژهٔ رمزگذاری پشتیبان (اختیاری)", help: "اگر تنظیم شود، همهٔ فایل‌های پشتیبان رمزگذاری می‌شوند و برای بازیابی همین گذرواژه لازم است. آن را جایی امن و خارج از سامانه نگه دارید؛ در صورت فراموشی، پشتیبان‌های رمزگذاری‌شده قابل بازیابی نیستند.", dir: "ltr", advanced: true },
          { key: "sync_interval_hours", label: "همگام‌سازی پنل‌ها: هر چند ساعت", type: "number", min: 1, max: 24 },
          { key: "guard_interval_minutes", label: "گارد کانال/گروه: هر چند دقیقه", type: "number", min: 1, max: 60 },
          { key: "rate_refresh_hours", label: "به‌روزرسانی نرخ آنلاین: هر چند ساعت", type: "number", min: 1, max: 24, when: (v) => v("rate_mode") === "auto" },
          { key: "log_retention_days", label: "نگه‌داری گزارش‌ها (روز)", type: "number", min: 7, max: 3650,
            help: "گزارش‌های قدیمی‌تر از این مدت (همگام‌سازی، ارسال پیام، و سوابق پایان‌یافتهٔ مسدودسازی) هر شبانه‌روز خودکار حذف می‌شوند تا دیتابیس سبک بماند. تاریخچهٔ مالی و فاکتورها هرگز حذف نمی‌شوند. پیش‌فرض ۹۰." },
          { key: "meter_retention_months", label: "نگه‌داری شمارشگرهای مصرف (ماه)", type: "number", min: 0, max: 120, advanced: true,
            help: "سوابقِ ماهانهٔ «متر مصرف» (مبنای محاسبهٔ مصرفِ مازاد و تمدید) قدیمی‌تر از این تعداد ماه پاک می‌شوند. ماهِ جاری همیشه می‌ماند. فاکتورها و تاریخچهٔ مالی حذف نمی‌شوند. ۰ = بدون پاک‌سازی. پیش‌فرض ۶." },
          { key: "owner_data_retention_days", label: "نگه‌داری فایل‌ها و کاربرانِ بلااستفاده (روز)", type: "number", min: 0, max: 3650, advanced: true,
            help: "تصویرِ رسیدهای پرداختِ بررسی‌شده، فایلِ PDF فاکتورها (که هر وقت لازم شد دوباره ساخته می‌شود) و کاربرانی که در ربات ثبت‌نام نکرده و مدت‌هاست غیرفعال‌اند، پس از این مدت پاک می‌شوند. فاکتورها، پرداخت‌ها و تاریخچهٔ مالی حذف نمی‌شوند. ۰ = غیرفعال. پیش‌فرض ۱۸۰." },
          { key: "daily_digest_enabled", label: "خلاصهٔ روزانه به تلگرام مالک",
            help: "هر روز یک پیامِ جمع‌بندی (آمار و سلامتِ سامانه) به چتِ خصوصیِ شما ارسال می‌شود." },
          { key: "daily_digest_hour", label: "ساعتِ خلاصهٔ روزانه", type: "number", min: 0, max: 23,
            help: "به وقتِ ایران. پیش‌فرض ۹ صبح.", when: (v) => !!v("daily_digest_enabled") },
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
      {
        title: "تست رایگان فروشگاه‌ها",
        fields: [
          { key: "storefront_trial_reset_enabled", label: "فعال‌سازی خودکار ماهانهٔ تست رایگان",
            help: "ابتدای هر ماه میلادی، تست رایگان همهٔ مشتریانِ همهٔ فروشگاه‌ها دوباره فعال می‌شود و به آن‌ها پیام داده می‌شود — بدون نیاز به کاری از سوی فروشنده. حجمِ این تست‌ها در فاکتور نماینده حساب نمی‌شود، یعنی هزینه‌اش با شماست؛ سقفِ هر تست را در بخش «فروشگاه» تعیین می‌کنید. خاموش = تست رایگان یک‌بار برای همیشه." },
          { key: "storefront_trial_reset_day", label: "فعال‌سازی خودکار: روز ماه", type: "number", min: 1, max: 28,
            help: "روزِ ماهِ میلادی. دو روزِ بعد از آن هم بررسی می‌شود تا اگر فروشگاهی جا مانده باشد جبران شود؛ فروشگاهی که همان ماه فعال شده دوباره فعال نمی‌شود.",
            when: (v) => !!v("storefront_trial_reset_enabled") },
          { key: "storefront_trial_reset_hour", label: "فعال‌سازی خودکار: ساعت", type: "number", min: 0, max: 23,
            help: "به وقتِ ایران. پیش‌فرض ۸ صبح.", when: (v) => !!v("storefront_trial_reset_enabled") },
        ],
      },
    ],
  },
  {
    id: "dunning",
    title: "یادآوری و مسدودسازی",
    icon: <NotificationsActiveIcon fontSize="small" />,
    description:
      "روزشماری از زمان ارسال فاکتور تا یادآوری‌ها و مسدودسازی. مسدودسازی واقعی به‌طور پیش‌فرض خاموش (آزمایشی) است تا زمانی که مطمئن نشده‌اید، کاربری مسدود نشود.",
    subs: [
      {
        fields: [
          { key: "reminder1_day", label: "یادآوری اول (روز پس از صدور)", type: "number", min: 0, max: 60 },
          { key: "reminder2_day", label: "یادآوری دوم (روز)", type: "number", min: 0, max: 60 },
          { key: "warning_day", label: "اخطار نهایی (روز)", type: "number", min: 0, max: 60 },
          { key: "enforcement_day", label: "مسدودسازی (روز)", type: "number", min: 0, max: 60 },
          { key: "enforcement_enabled", label: "مسدودسازی واقعی", help: "خاموش = فقط گزارش آزمایشی. روشن = کاربران نمایندهٔ بدهکار و زیرمجموعه‌هایش غیرفعال می‌شوند." },
          { key: "auto_restore_on_payment", label: "بازگردانی خودکار با پرداخت", help: "پس از تأیید پرداخت، نماینده و کاربرانش خودکار به حالت قبل برمی‌گردند." },
          { key: "enforcement_admin_lockout_enabled", label: "قفل ورود ادمین هنگام مسدودسازی", help: "هنگام مسدودسازیِ کامل، پس از غیرفعال‌شدن کاربران و صفرشدن سقف‌ها، برای خودِ نماینده و همهٔ زیرمجموعه‌هایش یک رمز روی پنل هیدیفای گذاشته می‌شود تا لینک UUID فعلی‌شان بسوزد و نتوانند وارد پنل شوند و کاربرانشان را دوباره فعال کنند. با پرداخت/بازگردانی، رمز به مقدار بازگردانی برمی‌گردد. روی «توقف ساخت کاربر» اعمال نمی‌شود." },
          { key: "enforcement_lock_password", label: "رمز قفل (هنگام مسدودسازی)", dir: "ltr", advanced: true,
            help: "این رمز روی همهٔ ادمین‌های زیردرختِ نمایندهٔ مسدودشده گذاشته می‌شود (مالک هرگز تغییر نمی‌کند). پیش‌فرض blocked-node." },
          { key: "enforcement_restore_password", label: "رمز بازگردانی (هنگام رفع مسدودی)", dir: "ltr", advanced: true,
            help: "هنگام رفع مسدودی/پرداخت، رمز ادمین‌ها به این مقدار برمی‌گردد. پیش‌فرض 123." },
          { key: "pending_payment_hold_days", label: "مهلت توقف یادآوری هنگام پرداختِ در انتظار (روز)", type: "number", min: 1, max: 365, advanced: true,
            help: "وقتی نماینده‌ای رسید/تراکنش فرستاده و منتظر تأیید شماست، یادآوری/مسدودسازیِ همان فاکتور تا این چند روز متوقف می‌شود تا یک رسیدِ بررسی‌نشده برای همیشه بدهی را پنهان نکند. پیش‌فرض ۷." },
          { key: "enforcement_action_batch_limit", label: "تعداد نماینده در هر دورِ مسدودسازی (هر پنل)", type: "number", min: 1, max: 20, advanced: true,
            help: "کارگرِ مسدودسازی در هر اجرا و برای هر پنل حداکثر این تعداد نماینده را پردازش می‌کند. پیش‌فرض ۳." },
          { key: "enforcement_panel_concurrency", label: "هم‌زمانیِ پنل‌ها در مسدودسازی", type: "number", min: 1, max: 20, advanced: true,
            help: "چند پنل به‌صورت هم‌زمان پردازش شوند. پنل‌ها مستقل‌اند، پس مسدودسازی/بازگردانیِ پنل‌های مختلف موازی پیش می‌رود؛ هر پنل به‌تنهایی ترتیبی می‌ماند (فشارِ هم‌زمان روی یک پنل نمی‌آید). پیش‌فرض ۶." },
          { key: "enforcement_worker_interval_minutes", label: "بازهٔ اجرای کارگرِ مسدودسازی (دقیقه)", type: "number", min: 1, max: 60, advanced: true,
            help: "هر چند دقیقه صف مسدودسازی/بازگردانی پردازش شود. پیش‌فرض ۵." },
          { key: "enforcement_user_chunk_size", label: "اندازهٔ دستهٔ کاربران در هر درخواست", type: "number", min: 1, max: 500, advanced: true,
            help: "کاربران در دسته‌های این‌اندازه به‌صورت گروهی فعال/غیرفعال می‌شوند (یک درخواست به پنل برای هر دسته). پیش‌فرض ۵۰۰." },
          { key: "enforcement_admin_chunk_size", label: "هم‌زمانیِ تنظیم محدودیت ادمین‌ها", type: "number", min: 1, max: 50, advanced: true,
            help: "حداکثر تعداد درخواستِ هم‌زمان برای صفر/بازگرداندنِ سقفِ ادمین‌ها. پیش‌فرض ۱۰." },
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
          { key: "owner_telegram", label: "آیدی تلگرام مالک", help: "مالکِ ربات را تعیین می‌کند. آیدیِ عددی (مثلاً 123456789) بلافاصله اعمال می‌شود؛ @username در نخستین /start همان حساب اعمال می‌شود.", dir: "ltr" },
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
    id: "usercreate",
    title: "ساخت کاربر",
    icon: <PersonIcon fontSize="small" />,
    note:
      "نماینده‌های اصلی می‌توانند از ربات، کاربر بسازند. گزینه‌های زیر (با کاما جدا شوند) منوی انتخابِ نماینده را می‌سازند.",
    subs: [
      {
        fields: [
          { key: "user_create_enabled", label: "فعال‌سازی ساخت کاربر از ربات" },
          { key: "user_create_gb_options", label: "حجم‌های مجاز (گیگابایت، با کاما)", help: "مثلاً: 20, 30, 50, 100", type: "csv", dir: "ltr" },
          { key: "user_create_day_options", label: "مدت‌های مجاز (روز، با کاما)", help: "مثلاً: 30, 60", type: "csv", dir: "ltr" },
          { key: "user_create_bulk_counts", label: "تعدادهای گروهی مجاز (با کاما)", help: "مثلاً: 5, 10, 20", type: "csv", dir: "ltr" },
        ],
      },
    ],
  },
  {
    id: "testconfig",
    title: "کانفیگ تست",
    icon: <ScienceIcon fontSize="small" />,
    note:
      "دکمهٔ «🧪 کانفیگ تست» در منوی مدیرِ ربات (یا دستور /test): با یک ضربه یک سرویسِ تست می‌سازد و لینک و QR آن را همان‌جا می‌فرستد. این کانفیگ‌ها با ادمینِ خودِ پنل ساخته می‌شوند، پس در فاکتور هیچ نماینده‌ای نمی‌آیند و هزینه‌شان با شماست.",
    subs: [
      {
        fields: [
          { key: "test_config_panel_id", label: "پنلِ ساختِ تست", type: "select", optionsFrom: "panels", numeric: true,
            help: "تست‌ها همیشه از همین پنل ساخته می‌شوند (هیچ‌وقت تصادفی). در ربات هم با دکمهٔ «🖥 تغییر پنل» قابل تغییر است." },
          { key: "test_config_gb", label: "حجم تست (گیگابایت)", type: "number", min: 1, max: 1000 },
          { key: "test_config_days", label: "مدت تست (روز)", type: "number", min: 1, max: 365 },
          { key: "test_config_name", label: "نام کانفیگ روی پنل", dir: "ltr",
            help: "با /test نامِ دلخواه هم می‌توانید برای یک مشتریِ خاص نامِ دیگری بدهید." },
        ],
      },
    ],
  },
  {
    id: "crm",
    title: "پیگیری نمایندگان",
    icon: <TrackChangesIcon fontSize="small" />,
    note:
      "این آستانه‌ها فقط تعیین می‌کنند هر نماینده در صفحهٔ «پیگیری» در کدام دسته بیفتد. " +
      "هیچ‌کدام روی فاکتور، مسدودسازی یا ارسال پیام اثر ندارند. «روز» از آخرین سرویسِ " +
      "قابل‌فاکتوری که فروخته شمرده می‌شود.",
    subs: [
      {
        title: "بی‌فروشی",
        fields: [
          { key: "crm_dormant_days", label: "بعد از چند روز بی‌فروشی «خوابیده» شود؟", type: "number", min: 1, max: 365, help: "پیش‌فرض ۱۴ روز. هرچه کمتر، فهرست پیگیری شلوغ‌تر ولی زودهنگام‌تر." },
          { key: "crm_churned_days", label: "بعد از چند روز «ریزش‌کرده» شود؟", type: "number", min: 1, max: 365, help: "پیش‌فرض ۴۵ روز. باید از عدد بالا بزرگ‌تر باشد." },
        ],
      },
      {
        title: "حساب‌های تازه",
        fields: [
          { key: "crm_never_active_min_age_days", label: "حداقل سن حساب برای برچسب «هرگز فعال نشده» (روز)", type: "number", min: 1, max: 365, help: "زیر این سن، نفروختن طبیعی است و «تازه‌وارد» شمرده می‌شود." },
          { key: "crm_onboarding_days", label: "تا چند روز «تازه‌وارد» بماند؟", type: "number", min: 1, max: 365, help: "در این مدت هیچ برچسب هشداری نمی‌گیرد." },
        ],
      },
      {
        title: "روند فروش",
        fields: [
          { key: "crm_declining_pct", label: "«رو به افول» زیر چند درصدِ میانگین ۳ ماه؟", type: "number", min: 1, max: 100, help: "فروش این ماه به کل ماه تعمیم داده می‌شود و با میانگین سه ماه قبل مقایسه می‌شود. پیش‌فرض ۵۰٪.", advanced: true },
          { key: "crm_growing_pct", label: "«در حال رشد» بالای چند درصد؟", type: "number", min: 100, max: 1000, help: "پیش‌فرض ۱۲۵٪.", advanced: true },
        ],
      },
      {
        fields: [
          { key: "crm_snooze_default_days", label: "مهلت پیش‌فرض تعویق بعد از هر پیگیری (روز)", type: "number", min: 1, max: 365, help: "هنگام ثبت پیگیری همین عدد از پیش انتخاب می‌شود؛ برای هر مورد قابل تغییر است." },
        ],
      },
    ],
  },
  {
    id: "storefront",
    title: "ربات فروشگاهی",
    icon: <StorefrontIcon fontSize="small" />,
    note:
      "این تنظیم‌ها روی ربات‌های فروشگاهیِ نماینده‌ها اثر می‌گذارند — همان ربات‌هایی که مشتریانِ نماینده با آن‌ها سرویس می‌خرند. اعلان‌ها یک بار در روز و با فاصله فرستاده می‌شوند.",
    subs: [
      {
        title: "اطلاع‌رسانی به مشتریان",
        fields: [
          { key: "storefront_expiry_notify_days", label: "یادآوری پیش از انقضا (روز)", type: "number", min: 0, max: 60,
            help: "چند روز مانده به پایان سرویس به مشتری یادآوری شود. ۰ = خاموش." },
          { key: "storefront_expired_notify_days", label: "پیام بازگشت پس از انقضا (روز)", type: "number", min: 0, max: 60,
            help: "فقط سرویس‌هایی که در این بازه منقضی شده‌اند پیام می‌گیرند — تا پیامِ بازگشت به‌یک‌باره برای انبوهی از مشتریانِ قدیمی ارسال نشود. ۰ = خاموش." },
          { key: "storefront_trial_ended_notify_days", label: "پیام پس از پایان تست رایگان (روز)", type: "number", min: 0, max: 60,
            help: "فقط تست‌هایی که در این بازه تمام شده‌اند پیامِ «تستِ رایگان به پایان رسید؛ برای ادامه یک پلن تهیه کنید» می‌گیرند. ۰ = خاموش." },
          { key: "storefront_usage_alert_percent", label: "هشدار مصرف حجم (درصد)", type: "number", min: 1, max: 100,
            help: "وقتی مصرف سرویس به این درصد رسید، هشدارِ «حجمِ سرویس شما رو به پایان است» ارسال می‌شود. پیش‌فرض ۸۰. سرویسِ منقضی‌شده هرگز این هشدار را نمی‌گیرد." },
          { key: "storefront_autorenew_fire_gb", label: "آستانهٔ تمدید خودکار — حجم (گیگابایت)", type: "number", min: 0, max: 100,
            help: "اگر مشتری «تمدید خودکار» را روشن کرده باشد، سرویس وقتی کمتر از این مقدار حجم برایش مانده باشد یک‌بار خودکار تمدید می‌شود. پیش‌فرض ۱." },
          { key: "storefront_autorenew_fire_days", label: "آستانهٔ تمدید خودکار — روز", type: "number", min: 0, max: 60,
            help: "یا وقتی کمتر از این تعداد روز تا انقضای سرویس مانده باشد. پیش‌فرض ۱ (روزِ آخر)." },
          { key: "storefront_autorenew_interval_minutes", label: "بازهٔ بررسیِ تمدید خودکار (دقیقه)", type: "number", min: 1, max: 1440, advanced: true,
            help: "هر چند دقیقه سرویس‌های «تمدید خودکارِ» رو به اتمام بررسی و در صورت نیاز تمدید شوند. پیش‌فرض ۱۵." },
        ],
      },
      {
        title: "نگهداری و پاک‌سازی",
        fields: [
          { key: "storefront_stale_customer_days", label: "پاک‌سازی مشتریانِ بی‌اثر (روز)", type: "number", min: 0, max: 3650, advanced: true,
            help: "مشتری‌ای که هیچ خرید/شارژی نداشته و این مدت غیرفعال بوده حذف می‌شود. سوابق مالی هرگز پاک نمی‌شوند. ۰ = خاموش." },
          { key: "storefront_delivery_retention_days", label: "نگهداری سوابق ارسال پیام (روز)", type: "number", min: 0, max: 3650, advanced: true },
          { key: "storefront_max_pending_topups", label: "حداکثر شارژِ در انتظارِ هر مشتری", type: "number", min: 1, max: 50, advanced: true,
            help: "جلوی انبوهِ درخواستِ شارژِ بی‌پاسخ از یک مشتری را می‌گیرد." },
        ],
      },
      {
        title: "تست رایگان",
        fields: [
          { key: "storefront_trial_max_gb", label: "سقف حجم تست رایگان (گیگابایت)", type: "number", min: 1, max: 100,
            help: "حجم تست رایگان در فاکتور نماینده حساب نمی‌شود، یعنی هزینه‌اش با شماست. این سقف تعیین می‌کند فروشنده حداکثر چه حجمی بتواند تست بگذارد — و فروشگاه‌هایی که از قبل بالاتر تنظیم شده‌اند هم هنگام ساخت به همین عدد محدود می‌شوند. چون این تست‌ها هر ماه دوباره فعال می‌شوند، این عدد هزینهٔ ماهانهٔ شماست: «زمان‌بندی کارهای خودکار ← تست رایگان فروشگاه‌ها»." },
        ],
      },
      {
        title: "کارکرد داخلی",
        fields: [
          { key: "storefront_pending_order_reaper_minutes", label: "بازهٔ بررسیِ سفارش‌های نیمه‌کاره (دقیقه)", type: "number", min: 1, max: 1440, advanced: true,
            help: "هر چند دقیقه سفارش‌هایی که نیمه‌کاره مانده‌اند تعیین‌تکلیف (تکمیل یا بازگشتِ وجه) شوند." },
          { key: "storefront_operation_lease_seconds", label: "مهلتِ هر عملیاتِ خرید/تمدید (ثانیه)", type: "number", min: 300, max: 3600, advanced: true },
          { key: "storefront_live_refresh_seconds", label: "بازهٔ به‌روزرسانیِ وضعیتِ زنده (ثانیه)", type: "number", min: 5, max: 3600, advanced: true },
          { key: "storefront_delivery_worker_interval_minutes", label: "بازهٔ ارسالِ پیام‌های صف‌شده (دقیقه)", type: "number", min: 1, max: 60, advanced: true },
        ],
      },
    ],
  },
  {
    id: "templates",
    title: "متن پیام‌ها",
    icon: <ChatBubbleOutlineIcon fontSize="small" />,
    description: "متن پیام‌های ربات. از متغیرهایی مثل {name}، {period}، {amount_toman} و {payment_instructions} استفاده کنید.",
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
          { key: "tpl_storefront_trial_reset", label: "اعلان ریست تست رایگان فروشگاه", type: "multiline" },
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
  // Feeds the dynamic selects (currently only the test-config panel). Same query key as the Panels
  // page, so it is served from the cache when the owner has already been there.
  const { data: panels = [] } = useQuery({ queryKey: ["panels"], queryFn: listPanels });
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
  // A numeric setting is staged as TEXT while it is being typed (see components/NumberField), so an
  // emptied or half-typed field is a normal intermediate state. It must never reach the API as 0 or
  // NaN — block the save instead, with the offending field already marked in error.
  const numericEdit = (key: string) =>
    typeof byKey[key]?.value === "number" && typeof edits[key] === "string";
  const badNumberKeys = Object.keys(edits).filter(
    (key) => numericEdit(key) && numberValue(edits[key]) === null);

  // Any non-hidden setting not covered by a curated field falls into a "متفرقه" section, so a
  // newly-added backend setting is never silently lost from the panel. This is a SAFETY NET, not
  // a destination: a setting showing up here means it still needs a real label and a real home in
  // SECTIONS above. Until someone does that, at least render it usably — a raw key with a text box
  // is unreadable, and it silently broke editing for booleans and numbers (a switch saved as the
  // string "true"). The type is inferred from the value the API returns.
  const sections = useMemo(() => {
    const covered = new Set<string>();
    SECTIONS.forEach((s) => s.subs.forEach((sub) => sub.fields.forEach((f) => covered.add(f.key))));
    const leftover: Field[] = data
      .filter((s: any) => !covered.has(s.key) && !HIDDEN.has(s.key))
      .map((s: any) => ({
        key: s.key,
        // key_like_this → «key like this»: still the key, but readable at a glance.
        label: s.key.replace(/_/g, " "),
        help: `کلید: ${s.key}`,
        type: inferFieldType(s.value),
        dir: typeof s.value === "string" ? ("ltr" as const) : undefined,
      }));
    if (!leftover.length) return SECTIONS;
    return [
      ...SECTIONS,
      {
        id: "misc",
        title: "متفرقه",
        icon: <TuneRoundedIcon fontSize="small" />,
        description:
          "تنظیم‌هایی که در سامانه وجود دارند ولی هنوز در بخش‌های بالا دسته‌بندی نشده‌اند — معمولاً قابلیت‌های تازه‌ای که به‌تازگی اضافه شده‌اند. اگر معنی یکی برایتان روشن نیست، بهتر است دست‌نخورده بماند.",
        subs: [{ fields: leftover }],
      } as Section,
    ];
  }, [data]);

  const save = useMutation({
    mutationFn: () => {
      const items = Object.entries(edits).map(([key, value]) => {
        if (byKey[key] && Array.isArray(byKey[key].value) && typeof value === "string")
          value = value.split(",").map((x) => parseFloat(x.trim())).filter((n) => !isNaN(n));
        else if (numericEdit(key)) value = numberValue(value);
        return { key, value };
      });
      return updateSettings(items);
    },
    onSuccess: () => { show("تنظیمات ذخیره شد"); setEdits({}); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const refreshRateM = useMutation({
    mutationFn: refreshRate,
    onSuccess: (r: any) => { show(`نرخ آنلاین به‌روزرسانی شد: ${Number(r?.rate || 0).toLocaleString("fa-IR")} تومان`); qc.invalidateQueries({ queryKey: ["settings"] }); },
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
    if (type === "select") {
      let options = f.optionsFrom === "panels"
        ? [{ value: "0", label: "— انتخاب نشده —" },
           ...panels.filter((p: any) => p.enabled)
             .map((p: any) => ({ value: String(p.id), label: p.name ? `${p.key} — ${p.name}` : p.key }))]
        : (f.options || []);
      // A saved value the list no longer offers (a panel that was deleted or disabled) would render
      // as an empty box — say so instead, so the owner knows their choice needs re-picking.
      if (v !== undefined && v !== null && v !== "" && !options.some((o) => o.value === String(v)))
        options = [...options, { value: String(v), label: `نامعتبر (#${v})` }];
      return (
        <TextField key={f.key} select label={f.label} value={String(v ?? "")} fullWidth size="small"
          helperText={f.help}
          onChange={(e) => setVal(f.key, f.numeric ? Number(e.target.value) : e.target.value)}>
          {options.map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
        </TextField>
      );
    }
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
      const staged = numericEdit(f.key) ? (edits[f.key] as string) : String(v ?? "");
      const parsed = numberValue(staged);
      const outOfRange = parsed !== null
        && ((f.min !== undefined && parsed < f.min) || (f.max !== undefined && parsed > f.max));
      return <NumberField key={f.key} label={f.label} value={staged} fullWidth size="small"
        allowDecimal allowNegative={(f.min ?? 0) < 0}
        error={parsed === null || outOfRange}
        helperText={[f.help, range].filter(Boolean).join(" — ") || undefined}
        onChange={(next) => setVal(f.key, next)} />;
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
      <Paper variant="outlined" sx={{
        p: { xs: 2, sm: 3 },
        backdropFilter: (t) => t.palette.mode === "dark" ? TIER1_BLUR.dark : TIER1_BLUR.light,
        WebkitBackdropFilter: (t) => t.palette.mode === "dark" ? TIER1_BLUR.dark : TIER1_BLUR.light,
      }}>
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
                    label={`نرخ آنلاین تتر: ${Number(byKey["toman_per_usdt_auto"].value).toLocaleString("fa-IR")} تومان`} />
                : <Chip color="warning" size="small" icon={<InfoOutlinedIcon />} label="نرخ آنلاین هنوز دریافت نشده" />}
              {byKey["toman_per_usdt_auto_at"]?.value && (
                <Typography variant="caption" color="text.secondary" dir="ltr">
                  {fmtDateTime(String(byKey["toman_per_usdt_auto_at"].value))}
                </Typography>
              )}
              {!!getVal("pay_ton_enabled") && (
                Number(byKey["ton_toman_auto"]?.value) > 0
                  ? <Chip color={getVal("ton_rate_mode") === "auto" ? "success" : "default"} size="small"
                      label={`نرخ آنلاین گرام: ${Number(byKey["ton_toman_auto"].value).toLocaleString("fa-IR")} تومان`} />
                  : <Chip color="warning" size="small" icon={<InfoOutlinedIcon />} label="نرخ آنلاین گرام هنوز دریافت نشده" />
              )}
              {!!getVal("pay_avax_enabled") && (
                Number(byKey["avax_toman_auto"]?.value) > 0
                  ? <Chip color={getVal("avax_rate_mode") === "auto" ? "success" : "default"} size="small"
                      label={`نرخ آنلاین اوالانچ: ${Number(byKey["avax_toman_auto"].value).toLocaleString("fa-IR")} تومان`} />
                  : <Chip color="warning" size="small" icon={<InfoOutlinedIcon />} label="نرخ آنلاین اوالانچ هنوز دریافت نشده" />
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
        sx={{
          position: "sticky", top: 0, zIndex: 3, mb: 2, py: 1.25, px: 2, gap: 1.5, flexWrap: "wrap",
          borderRadius: 3,
          // frosted tier-2 glass (shows the ambient colour through) instead of a flat grey rectangle
          bgcolor: (t) => t.palette.mode === "dark" ? TIER2_BG.dark : TIER2_BG.light,
          backdropFilter: TIER2_BLUR,
          WebkitBackdropFilter: TIER2_BLUR,
          border: "1px solid", borderColor: "divider",
          boxShadow: (t) => t.palette.mode === "dark"
            ? "0 8px 24px -14px rgba(0,0,0,.65)" : "0 8px 24px -16px rgba(0,0,0,.22)",
        }}>
        <Box>
          <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>تنظیمات سامانه</Typography>
          <Typography variant="caption" color="text.secondary">
            پیکربندی ربات، پرداخت، قیمت و زمان‌بندی — مقادیر حساس رمزنگاری می‌شوند.
          </Typography>
        </Box>
        <Button variant="contained" onClick={() => save.mutate()}
          disabled={save.isPending || dirtyCount === 0 || badNumberKeys.length > 0}>
          ذخیره تغییرات{dirtyCount ? ` (${dirtyCount})` : ""}
        </Button>
      </Stack>

      <Box sx={{ display: "flex", flexDirection: compact ? "column" : "row", gap: 2, alignItems: "flex-start" }}>
        <Paper variant="outlined" sx={{ flexShrink: 0, width: compact ? "100%" : 240, position: compact ? "static" : "sticky", top: 80, overflow: "hidden", backdropFilter: (t) => t.palette.mode === "dark" ? TIER1_BLUR.dark : TIER1_BLUR.light, WebkitBackdropFilter: (t) => t.palette.mode === "dark" ? TIER1_BLUR.dark : TIER1_BLUR.light }}>
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
