import { useState, useMemo } from "react";
import {
  Box, Button, Card, CardContent, Typography, TextField, Switch, FormControlLabel,
  Stack, Divider, Accordion, AccordionSummary, AccordionDetails,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listSettings, updateSettings } from "../api/client";
import { useToast, errMsg } from "../components/Toast";

const GROUP_FA: Record<string, string> = {
  telegram: "تلگرام", payments: "روش‌های پرداخت", pricing: "قیمت‌گذاری",
  schedule: "زمان‌بندی کارهای خودکار", dunning: "یادآوری و مسدودسازی", templates: "متن پیام‌ها",
  general: "عمومی", deploy: "دامنه و HTTPS (هنگام نصب روی سرور)",
};
const GROUP_NOTE: Record<string, string> = {
  telegram: "برای ثبت کانال/گروه (حتی خصوصی): ربات را در آن ادمین کنید، سپس یک پیام از همان کانال/گروه را برای ربات فوروارد کنید تا شناسه‌اش ثبت شود. بعد کلید «عضویت اجباری» مربوطه را روشن کنید. اگر هر دو روشن باشند، کاربر باید عضو هر دو باشد. «پاک‌سازی واقعی» با همین یک کلید، هم کانال و هم گروه را از افرادی که نمایندهٔ فعال نیستند پاک می‌کند (روزانه).",
  payments: "روش‌های پرداختی که روشن باشند روی فاکتور و در «پرداخت» ربات به نماینده نشان داده می‌شوند. روشی که اطلاعاتش (آدرس کیف پول یا شماره کارت) خالی باشد، نمایش داده نمی‌شود.",
  schedule: "همهٔ ساعت‌ها به وقت ایران است. کارهای «هر چند ساعت/دقیقه» روی ساعت‌های رُند اجرا می‌شوند (مثلاً هر ۲ ساعت = ۰۰، ۰۲، …، ۲۲) و به زمان دیپلوی وابسته نیستند. برای فاصله‌گذاری کاملاً یکنواخت، عددی بگذارید که ۲۴ (برای ساعت) یا ۶۰ (برای دقیقه) بر آن بخش‌پذیر باشد: ۱، ۲، ۳، ۴، ۶، ۸، ۱۲. تغییرات بلافاصله و بدون نیاز به ری‌استارت اعمال می‌شوند.",
  deploy: "این مقادیر هنگام نصب روی سرور (فاز ۲) استفاده می‌شوند: دامنه را وارد کنید، رکورد A آن را به IP سرور بدهید، و نصب‌کننده به‌صورت خودکار گواهی SSL را می‌گیرد و تمدید می‌کند.",
};
const LABELS: Record<string, string> = {
  telegram_bot_token: "توکن ربات", announcement_channel_id: "شناسه کانال", announcement_channel_link: "لینک کانال (اختیاری، برای کانال خصوصی)",
  channel_membership_required: "عضویت اجباری کانال",
  announcement_group_id: "شناسه گروه", announcement_group_link: "لینک گروه (اختیاری، برای گروه خصوصی)",
  group_membership_required: "عضویت اجباری گروه",
  channel_kick_enabled: "پاک‌سازی واقعی کانال و گروه (خاموش=آزمایشی)", kick_grace_minutes: "مهلت ارفاق پاک‌سازی (دقیقه)", one_time_invite_links: "لینک عضویت یک‌بارمصرف",
  usdt_bep20_address: "آدرس کیف پول USDT", usdt_bep20_contract: "قرارداد USDT", bscscan_api_key: "کلید API بی‌اسکن",
  bscscan_api_url: "آدرس API بی‌اسکن", usdt_master_xpub: "xpub کیف پول مادر", min_confirmations: "حداقل تأیید",
  payment_amount_tolerance_usdt: "اغماض مبلغ (USDT)",
  pay_usdt_enabled: "روش پرداخت: USDT (کیف پول + TXID)", pay_screenshot_enabled: "روش پرداخت: ارسال تصویر رسید",
  pay_card_enabled: "روش پرداخت: کارت‌به‌کارت", card_number: "شماره کارت", card_holder_name: "نام صاحب کارت",
  default_price_per_gb: "قیمت پیش‌فرض هر گیگ (تومان)", toman_per_usdt: "نرخ تبدیل (تومان به ازای هر USDT)",
  rate_mode: "حالت نرخ", excluded_usage_gb: "حجم‌های معاف (گیگ، با کاما)", min_sale_toman: "حداقل فروش هر نماینده (تومان، ۰=غیرفعال)",
  invoice_day_of_month: "صدور فاکتور ماهانه: روز ماه (۱ تا ۲۸)", invoice_hour: "صدور فاکتور ماهانه: ساعت (۰ تا ۲۳)",
  dunning_hour: "یادآوری/مسدودسازی روزانه: ساعت (۰ تا ۲۳)",
  sync_interval_hours: "سینک پنل‌ها: هر چند ساعت",
  guard_interval_minutes: "گارد کانال/گروه: هر چند دقیقه",
  backup_enabled: "بکاپ خودکار به تلگرام (روشن/خاموش)", backup_interval_hours: "بکاپ خودکار: هر چند ساعت",
  reminder1_day: "یادآوری اول (روز)", reminder2_day: "یادآوری دوم (روز)", warning_day: "اخطار (روز)",
  enforcement_day: "مسدودسازی (روز)", enforcement_enabled: "مسدودسازی واقعی (خاموش = آزمایشی)",
  auto_restore_on_payment: "بازگردانی خودکار با پرداخت",
  owner_name: "نام مالک", owner_telegram: "تلگرام مالک",
  server_domain: "دامنه سرور", https_enabled: "فعال‌سازی HTTPS خودکار", acme_email: "ایمیل برای گواهی SSL",
  tpl_welcome: "خوش‌آمد", tpl_membership: "نیاز به عضویت", tpl_menu: "منو",
  tpl_link_matched: "ثبت لینک موفق", tpl_link_not_found: "لینک نامعتبر", tpl_invoice: "فاکتور",
  tpl_reminder1: "یادآوری اول", tpl_reminder2: "یادآوری دوم", tpl_warning: "اخطار نهایی",
  tpl_payment_received: "تأیید پرداخت",
};
const GROUP_ORDER = ["telegram", "payments", "pricing", "schedule", "dunning", "general", "deploy", "templates"];
// Numeric bounds for the schedule fields — mirror the backend clamp (app.scheduler.jobs.load_config)
// so the panel can't show a value that silently differs from what actually runs.
const BOUNDS: Record<string, [number, number]> = {
  invoice_day_of_month: [1, 28], invoice_hour: [0, 23], dunning_hour: [0, 23],
  sync_interval_hours: [1, 23], guard_interval_minutes: [1, 59], backup_interval_hours: [1, 23],
};

export default function Settings() {
  const qc = useQueryClient();
  const { node, show } = useToast();
  const { data = [] } = useQuery({ queryKey: ["settings"], queryFn: listSettings });
  const [edits, setEdits] = useState<Record<string, any>>({});

  const grouped = useMemo(() => {
    const g: Record<string, any[]> = {};
    for (const s of data) (g[s.group] ||= []).push(s);
    return g;
  }, [data]);

  const setVal = (key: string, value: any) => setEdits((e) => ({ ...e, [key]: value }));
  const valueOf = (s: any) => (s.key in edits ? edits[s.key] : s.value);

  const save = useMutation({
    mutationFn: () => {
      const items = Object.entries(edits).map(([key, value]) => {
        if (key === "excluded_usage_gb" && typeof value === "string")
          value = value.split(",").map((x) => parseInt(x.trim())).filter((n) => !isNaN(n));
        return { key, value };
      });
      return updateSettings(items);
    },
    onSuccess: () => { show("تنظیمات ذخیره شد"); setEdits({}); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const field = (s: any) => {
    const label = LABELS[s.key] || s.key;
    const v = valueOf(s);
    if (typeof s.value === "boolean")
      return <FormControlLabel key={s.key} control={<Switch checked={!!v} onChange={(e) => setVal(s.key, e.target.checked)} />} label={label} />;
    if (s.key.startsWith("tpl_"))
      return <TextField key={s.key} label={label} value={v ?? ""} multiline minRows={2} fullWidth onChange={(e) => setVal(s.key, e.target.value)} />;
    if (s.key === "excluded_usage_gb")
      return <TextField key={s.key} label={label} value={Array.isArray(v) ? v.join(", ") : v ?? ""} fullWidth onChange={(e) => setVal(s.key, e.target.value)} />;
    if (s.is_secret)
      return <TextField key={s.key} label={label} placeholder={s.has_value ? "•••• (برای تغییر وارد کنید)" : ""} dir="ltr"
        value={s.key in edits ? edits[s.key] : ""} fullWidth onChange={(e) => setVal(s.key, e.target.value)} />;
    if (typeof s.value === "number") {
      const b = BOUNDS[s.key];
      return <TextField key={s.key} label={label} type="number" value={v ?? 0} fullWidth
        inputProps={b ? { min: b[0], max: b[1] } : undefined}
        helperText={b ? `مجاز: ${b[0]} تا ${b[1]}` : undefined}
        onChange={(e) => setVal(s.key, Number(e.target.value))} />;
    }
    return <TextField key={s.key} label={label} value={v ?? ""} fullWidth dir={s.key.includes("address") || s.key.includes("link") ? "ltr" : undefined}
      onChange={(e) => setVal(s.key, e.target.value)} />;
  };

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="body2" color="text.secondary">پیکربندی سامانه (مقادیر حساس رمزنگاری می‌شوند)</Typography>
        <Button variant="contained" onClick={() => save.mutate()} disabled={save.isPending || Object.keys(edits).length === 0}>
          ذخیره تغییرات ({Object.keys(edits).length})
        </Button>
      </Stack>

      {GROUP_ORDER.filter((g) => grouped[g]).map((g) => (
        <Accordion key={g} defaultExpanded={g !== "templates"} sx={{ mb: 1 }}>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Typography sx={{ fontWeight: 600 }}>{GROUP_FA[g] || g}</Typography>
          </AccordionSummary>
          <AccordionDetails>
            {GROUP_NOTE[g] && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                {GROUP_NOTE[g]}
              </Typography>
            )}
            <Stack spacing={2}>{grouped[g].map(field)}</Stack>
          </AccordionDetails>
        </Accordion>
      ))}
      {node}
    </Box>
  );
}
