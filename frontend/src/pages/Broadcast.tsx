import { useEffect, useMemo, useState } from "react";
import {
  Box, Card, CardContent, Typography, TextField, Button, Stack, Alert, MenuItem, Select,
  Chip, Divider, Table, TableBody, TableCell, TableHead, TableRow, Collapse,
  FormControlLabel, Switch, Link, Tooltip, IconButton, ListSubheader,
  Dialog, DialogTitle, DialogContent, DialogActions, Checkbox, CircularProgress,
} from "@mui/material";
import CampaignIcon from "@mui/icons-material/esm/Campaign";
import CleaningServicesIcon from "@mui/icons-material/esm/CleaningServices";
import GroupIcon from "@mui/icons-material/esm/Group";
import PublicIcon from "@mui/icons-material/esm/Public";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import NotificationsActiveIcon from "@mui/icons-material/esm/NotificationsActive";
import DeleteForeverIcon from "@mui/icons-material/esm/DeleteForever";
import { useMutation, useQuery } from "@tanstack/react-query";
import { broadcastMessage, broadcastPreview, broadcastStatus, panelMigration, panelMigrationPreview, runChannelGuard, listPanels, highVolumeUsers, highVolumeWarn, highVolumeDelete } from "../api/client";
import type { HighVolumeDeleteResult, HighVolumeDeleteRow } from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { DataState } from "../components/DataState";
import { telegramHref } from "../components/TelegramLink";
import { fmtNum, fmtToman } from "../format";
import { NumberField } from "../components/NumberField";

// Audience filters — each is applied ON TOP of the base set (resellers in the main «نمایندگان»
// list that are not exempt from billing and are on an active panel). The panel filter is combinable.
// Grouped by lifecycle so the owner targets exactly the right stage (debt state / access / activity).
const AUDIENCES: { value: string; label: string; group: string; hint?: string; threshold?: { label: string; def: number } }[] = [
  { value: "all", label: "همهٔ نمایندگان", group: "عمومی" },

  { value: "debtors", label: "بدهکاران (هر فاکتورِ پرداخت‌نشده)", group: "بدهی و پرداخت" },
  { value: "overdue", label: "سررسیدگذشته‌ها (فاکتورِ معوق)", group: "بدهی و پرداخت",
    hint: "فاکتورهایی که واردِ چرخهٔ یادآوری شده‌اند و اکنون سررسیدشان گذشته است" },
  { value: "deferred", label: "مهلت‌دارها (هنوز به مهلت نرسیده)", group: "بدهی و پرداخت",
    hint: "بدهکارانی که مهلتِ پرداخت گرفته‌اند و مهلتشان هنوز نرسیده است" },
  { value: "pending_payment", label: "پرداختِ در انتظارِ تأیید", group: "بدهی و پرداخت",
    hint: "رسید/تراکنش ارسال کرده‌اند و منتظرِ تأییدِ شما هستند" },
  { value: "paid_up", label: "خوش‌حساب‌ها (بدونِ بدهی)", group: "بدهی و پرداخت",
    hint: "هیچ فاکتورِ بازی ندارند — مناسبِ تشکر یا پیشنهادِ ویژه" },

  { value: "enforced", label: "معلق‌شده‌ها", group: "وضعیتِ دسترسی" },
  { value: "frozen", label: "فریزشده‌ها (توقفِ ساختِ کاربر)", group: "وضعیتِ دسترسی" },

  { value: "zero_sale", label: "فروش صفرِ این ماه", group: "فروش و فعالیت" },
  { value: "invoice_below", label: "فاکتورِ این ماه زیرِ مبلغ", group: "فروش و فعالیت",
    threshold: { label: "فاکتور کم‌تر از این مبلغ (تومان)", def: 100000 } },
  { value: "invoice_above", label: "فاکتورِ این ماه بالای مبلغ (VIP)", group: "فروش و فعالیت",
    threshold: { label: "فاکتور دست‌کم این مبلغ (تومان)", def: 1000000 } },
  { value: "few_active", label: "کم‌تر از تعدادِ مشخص کاربرِ فعال", group: "فروش و فعالیت",
    threshold: { label: "کم‌تر از این تعداد کاربرِ فعال", def: 10 } },
  { value: "new_resellers", label: "تازه‌واردها (ثبت‌نامِ اخیر)", group: "فروش و فعالیت",
    threshold: { label: "ثبت‌نام در این چند روزِ اخیر", def: 30 } },
];
const AUDIENCE_GROUPS = [...new Set(AUDIENCES.map((a) => a.group))];

const STATUS_FA: Record<string, string> = {
  sent: "ارسال شد", blocked: "مسدود کرده", failed: "ناموفق", unregistered: "بدون ربات", pending: "—",
};

// Outcome of the high-volume delete, per user (mirrors services.end_user_delete.DeleteStatus).
// Everything except the first two means the local rows were deliberately KEPT.
const HV_DEL_FA: Record<string, string> = {
  deleted: "از پنل حذف و از سامانه پاک شد",
  already_absent: "از قبل روی پنل نبود؛ از سامانه پاک شد",
  owner_mismatch: "روی پنل هست ولی دیگر متعلق به این نماینده نیست — پاک نشد",
  lookup_failed: "خطا در ارتباط با پنل — چیزی پاک نشد",
  delete_failed: "حذف از پنل ناموفق بود — چیزی پاک نشد",
  verify_failed: "حذف از پنل تأیید نشد — چیزی پاک نشد",
  skipped_low_quota: "حجمش کمتر از حدِ مجازِ این ابزار است — رد شد",
  skipped_no_owner: "نمایندهٔ سازنده‌اش مشخص نیست — رد شد",
  skipped_storefront: "سرویسِ فروخته‌شدهٔ فروشگاه است؛ از بخشِ فروشگاه اقدام کنید",
  panel_unavailable: "پنلِ این کاربر در دسترس نیست",
  not_found: "این کاربر دیگر در سامانه نیست",
};
const STATUS_COLOR: Record<string, any> = {
  sent: "success", blocked: "warning", failed: "error", unregistered: "default", pending: "default",
};

export default function Broadcast() {
  const { node, show } = useToast();
  const [text, setText] = useState("");
  const [audience, setAudience] = useState("all");
  const [panelId, setPanelId] = useState<string>("");
  const [threshold, setThreshold] = useState<string>("");
  const [report, setReport] = useState<any>(null);   // last preview result (recipient list)
  const [showList, setShowList] = useState(true);
  const [polling, setPolling] = useState(false);     // poll live progress after a send starts
  const { data: panels = [] } = useQuery({ queryKey: ["panels"], queryFn: listPanels });

  // Live progress of the background send (in-memory snapshot; no DB). Poll while a run is active.
  const { data: status } = useQuery({
    queryKey: ["broadcast-status"],
    queryFn: broadcastStatus,
    enabled: polling,
    refetchInterval: polling ? 2000 : false,
  });
  // Stop polling once the run reports finished.
  useEffect(() => {
    if (polling && status && status.running === false && status.finished_at) setPolling(false);
  }, [polling, status]);

  const audDef = AUDIENCES.find((a) => a.value === audience);
  const needsThreshold = !!audDef?.threshold;

  const body = useMemo(() => ({
    text, audience,
    panel_id: panelId ? Number(panelId) : undefined,
    threshold: needsThreshold ? Number(threshold || audDef!.threshold!.def) : undefined,
  }), [text, audience, panelId, threshold, needsThreshold, audDef]);

  const preview = useMutation({
    mutationFn: () => broadcastPreview(body),
    onSuccess: (r: any) => { setReport({ ...r, _sent: false }); setShowList(true); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const send = useMutation({
    mutationFn: () => broadcastMessage(body),
    onSuccess: (r: any) => {
      show(
        `ارسال در پس‌زمینه شروع شد — به ${fmtNum(r.total)} نماینده`
        + (r.unregistered ? ` (${fmtNum(r.unregistered)} بدون ربات، پیام دریافت نمی‌کنند)` : "")
        + "؛ خلاصهٔ نتیجه به تلگرامِ شما می‌رسد.",
        "success");
      setText("");
      if (r.total > 0) setPolling(true);   // show live progress while the send runs
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  const guard = useMutation({
    mutationFn: () => runChannelGuard(),
    onSuccess: (r: any) => {
      if (r.skipped) show("کانال تنظیم نشده است", "info");
      else show(
        r.dry_run
          ? `حالت آزمایشی: ${r.in_channel_non_reseller} کاربر غیرنماینده در کانال (هیچ‌کس حذف نشد)`
          : `${r.kicked} کاربر غیرنماینده از کانال حذف شد`,
        r.dry_run ? "info" : "success");
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  // ── «هشدارِ کاربرانِ پرحجم» (the 1000 GB mistake) ──
  const [hvThreshold, setHvThreshold] = useState("");
  const [hvPanelId, setHvPanelId] = useState("");
  const [hvThisMonth, setHvThisMonth] = useState(true);
  const [hvData, setHvData] = useState<any>(null);
  const [hvShow, setHvShow] = useState(true);
  const hvParams = () => ({
    threshold: hvThreshold ? Number(hvThreshold) : undefined,
    panel_id: hvPanelId ? Number(hvPanelId) : undefined,
    this_month_only: hvThisMonth,
  });
  const hvCheck = useMutation({
    mutationFn: () => highVolumeUsers(hvParams()),
    onSuccess: (r: any) => { setHvData(r); setHvShow(true); if (!hvThreshold && r.threshold) setHvThreshold(String(r.threshold)); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const hvWarnAll = useMutation({
    mutationFn: () => highVolumeWarn(hvParams()),
    onSuccess: (r: any) => show(`هشدارها در پس‌زمینه ارسال شد — به ${fmtNum(r.total)} نماینده${r.unregistered ? ` (${fmtNum(r.unregistered)} بدون ربات)` : ""}؛ خلاصه به تلگرامِ شما می‌رسد.`, "success"),
    onError: (e) => show(errMsg(e), "error"),
  });
  const hvWarnOne = useMutation({
    mutationFn: (rootId: number) => highVolumeWarn({ ...hvParams(), root_reseller_ids: [rootId] }),
    onSuccess: () => show("هشدار به این نماینده ارسال شد؛ خلاصه به تلگرامِ شما می‌رسد.", "success"),
    onError: (e) => show(errMsg(e), "error"),
  });
  const hvRows = hvData?.rows || [];

  // Delete a mistaken user from the panel AND from billing. One row = single, N = the whole table.
  const [hvDelTargets, setHvDelTargets] = useState<any[] | null>(null);
  const [hvDelAck, setHvDelAck] = useState(false);   // bulk-only "I understand" gate
  const [hvDelResult, setHvDelResult] = useState<HighVolumeDeleteResult | null>(null);
  const hvDelete = useMutation({
    // Sent in small sequential chunks: each user costs two panel round-trips plus a share of a
    // heavy CSRF page load, so one big request would sit near the proxy's read timeout.
    mutationFn: async (rows: any[]) => {
      const ids = rows.map((r) => r.snapshot_id);
      const acc: HighVolumeDeleteResult = {
        deleted: 0, already_absent: 0, purged: 0, skipped: 0, failed: 0,
        meters_deleted: 0, rows: [],
      };
      for (let i = 0; i < ids.length; i += 25) {
        const r = await highVolumeDelete({ snapshot_ids: ids.slice(i, i + 25), ...hvParams() });
        acc.deleted += r.deleted; acc.already_absent += r.already_absent;
        acc.purged += r.purged; acc.skipped += r.skipped; acc.failed += r.failed;
        acc.meters_deleted += r.meters_deleted; acc.rows.push(...r.rows);
      }
      return acc;
    },
    onSuccess: (r) => {
      setHvDelResult(r);
      setHvDelTargets(null);
      setHvDelAck(false);
      show(
        `${fmtNum(r.purged)} کاربر از سابقهٔ سامانه پاک شد` +
        (r.failed ? ` — ${fmtNum(r.failed)} ناموفق` : "") +
        (r.skipped ? ` — ${fmtNum(r.skipped)} رد شد` : ""),
        r.failed ? "warning" : "success",
      );
      hvCheck.mutate();   // re-read from the server rather than trusting a local splice
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  // ── «اعلامِ آدرسِ جدیدِ پنل» (personalized per-reseller links) ──
  const [migPanelId, setMigPanelId] = useState("");
  const [migPrevHost, setMigPrevHost] = useState("");
  const [migPreview, setMigPreview] = useState<any>(null);
  const migPrev = useMutation({
    mutationFn: () => panelMigrationPreview({ panel_id: Number(migPanelId), previous_host: migPrevHost || undefined }),
    onSuccess: (r: any) => { setMigPreview(r); if (!migPrevHost && r.previous_host) setMigPrevHost(r.previous_host); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const migSend = useMutation({
    mutationFn: () => panelMigration({ panel_id: Number(migPanelId), previous_host: migPrevHost || undefined }),
    onSuccess: (r: any) => {
      show(`اعلامِ آدرسِ جدید در پس‌زمینه شروع شد — به ${fmtNum(r.total)} نماینده؛ خلاصه به تلگرامِ شما می‌رسد.`, "success");
      if (r.total > 0) setPolling(true);
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  // Reset the stale report whenever the filter changes (so it never mismatches the controls).
  const onFilterChange = (fn: () => void) => { fn(); setReport(null); };
  const rows = report ? [...(report.recipients || []), ...(report.skipped || [])] : [];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <CampaignIcon color="primary" />
            <Typography variant="h6">پیام همگانی</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            پایهٔ همهٔ فیلترها: نماینده‌هایِ «فهرستِ اصلی» که از فاکتور معاف نیستند و پنلِ فعال دارند.
            فیلتر و پنل را انتخاب کنید، «پیش‌نمایشِ گیرندگان» را بزنید تا دقیقاً ببینید پیام به چه کسانی می‌رسد، سپس ارسال کنید.
            ارسال در <b>پس‌زمینه</b> انجام می‌شود؛ پیشرفتِ زنده همین‌جا نمایش داده می‌شود و <b>خلاصهٔ نهایی به تلگرامِ شما</b> می‌رسد.
          </Typography>

          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }} useFlexGap flexWrap="wrap">
            <Select size="small" value={audience}
              onChange={(e) => onFilterChange(() => setAudience(e.target.value))}
              renderValue={(v) => AUDIENCES.find((a) => a.value === v)?.label ?? v}
              sx={{ minWidth: 260, "& .MuiSelect-select": { py: "7px !important" } }}>
              {AUDIENCE_GROUPS.flatMap((g) => [
                <ListSubheader key={`h-${g}`} sx={{ lineHeight: "32px", fontWeight: 700 }}>{g}</ListSubheader>,
                ...AUDIENCES.filter((a) => a.group === g).map((a) => (
                  <MenuItem key={a.value} value={a.value}>{a.label}</MenuItem>
                )),
              ])}
            </Select>
            <Select size="small" value={panelId} displayEmpty
              onChange={(e) => onFilterChange(() => setPanelId(e.target.value))}
              renderValue={(v) => v ? (panels.find((p: any) => String(p.id) === String(v))?.key ?? v) : "همهٔ پنل‌ها"}
              sx={{ minWidth: 160, "& .MuiSelect-select": { py: "7px !important" } }}>
              <MenuItem value="">همهٔ پنل‌ها</MenuItem>
              {panels.map((p: any) => <MenuItem key={p.id} value={p.id}>{p.key}</MenuItem>)}
            </Select>
            {needsThreshold && (
              <NumberField size="small" sx={{ minWidth: 220 }}
                label={audDef!.threshold!.label}
                value={threshold} placeholder={String(audDef!.threshold!.def)}
                onChange={(value) => onFilterChange(() => setThreshold(value))} />
            )}
            <Button variant="outlined" startIcon={<GroupIcon />} disabled={preview.isPending}
              onClick={() => preview.mutate()}>پیش‌نمایشِ گیرندگان</Button>
          </Stack>

          {audDef?.hint && (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: -1, mb: 2 }}>
              ℹ️ {audDef.hint}
            </Typography>
          )}

          {report && (
            <Alert severity="info" sx={{ mb: 2 }}
              action={rows.length
                ? <Button color="inherit" size="small" onClick={() => setShowList((s) => !s)}>{showList ? "بستن فهرست" : "نمایش فهرست"}</Button>
                : undefined}>
              این فیلتر <b>{fmtNum(report.matched)}</b> نماینده را شامل می‌شود: {fmtNum(report.total)} قابل‌ارسال{report.unregistered ? ` • ${fmtNum(report.unregistered)} بدون ربات (قابل‌دسترس نیستند)` : ""}.
            </Alert>
          )}

          {status && (status.running || status.finished_at) && (
            <Alert severity={status.running ? "info" : "success"} sx={{ mb: 2 }}>
              {status.running
                ? <>در حال ارسال… <b>{fmtNum(status.sent + status.blocked + status.failed)}</b> از {fmtNum(status.total)} (✅ {fmtNum(status.sent)} • 🚫 {fmtNum(status.blocked)} مسدود • ❌ {fmtNum(status.failed)} ناموفق)</>
                : <>آخرین ارسال تمام شد — ✅ <b>{fmtNum(status.sent)}</b> موفق، 🚫 {fmtNum(status.blocked)} مسدود، ❌ {fmtNum(status.failed)} ناموفق{status.unregistered ? ` • 📵 ${fmtNum(status.unregistered)} بدون ربات` : ""} (از {fmtNum(status.total)} گیرنده){status.duration_s != null ? ` • ${fmtNum(Math.round(status.duration_s))} ثانیه` : ""}. خلاصه به تلگرامِ شما هم ارسال شد.</>}
            </Alert>
          )}

          {preview.isPending && <DataState isLoading rows={3}><Box /></DataState>}
          {report && (
            <Collapse in={showList} unmountOnExit>
              <Box sx={{ mb: 2, maxHeight: 320, overflow: "auto", border: 1, borderColor: "divider", borderRadius: 2 }}>
                <Table size="small" stickyHeader className="resp-table">
                  <TableHead>
                    <TableRow><TableCell>نماینده</TableCell><TableCell>پنل</TableCell><TableCell>وضعیت</TableCell></TableRow>
                  </TableHead>
                  <TableBody>
                    {rows.map((r: any) => (
                      <TableRow key={r.reseller_id} hover>
                        <TableCell>{r.name}</TableCell>
                        <TableCell>{r.panel}</TableCell>
                        <TableCell><Chip size="small" color={STATUS_COLOR[r.status]} label={STATUS_FA[r.status] || r.status} /></TableCell>
                      </TableRow>
                    ))}
                    {rows.length === 0 && (
                      <TableRow><TableCell colSpan={3} align="center" sx={{ py: 3, color: "text.secondary" }}>هیچ نماینده‌ای با این فیلتر پیدا نشد</TableCell></TableRow>
                    )}
                  </TableBody>
                </Table>
              </Box>
            </Collapse>
          )}

          <Divider sx={{ mb: 2 }} />
          <TextField label="متن پیام" value={text} onChange={(e) => setText(e.target.value)}
            multiline minRows={5} fullWidth sx={{ mb: 2 }} />
          <Button variant="contained" startIcon={<CampaignIcon />}
            disabled={!text.trim() || send.isPending}
            onClick={() => {
              const n = report && !report._sent ? report.total : null;
              if (window.confirm(n != null
                ? `پیام به ${n} نماینده ارسال شود؟`
                : "پیام همگانی ارسال شود؟ (برای دیدنِ گیرندگان ابتدا «پیش‌نمایش» را بزنید)"))
                send.mutate();
            }}>ارسال</Button>
        </CardContent>
      </Card>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <PublicIcon color="primary" />
            <Typography variant="h6">اعلامِ آدرسِ جدیدِ پنل</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            به هر نمایندهٔ ثبت‌شدهٔ یک پنل، پیامی با <b>لینکِ اختصاصیِ خودش</b> (آدرسِ جدید + آدرسِ قبلی) ارسال می‌شود.
            متنِ پیام ثابت و آماده است و نیازی به نوشتنِ آن ندارید. ابتدا در «پنل‌ها» با «انتقال دامنه» دامنهٔ قبلی را ثبت کنید.
          </Typography>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }} useFlexGap flexWrap="wrap">
            <Select size="small" value={migPanelId} displayEmpty
              onChange={(e) => { setMigPanelId(e.target.value); setMigPrevHost(""); setMigPreview(null); }}
              renderValue={(v) => v ? (panels.find((p: any) => String(p.id) === String(v))?.key ?? v) : "انتخابِ پنل"}
              sx={{ minWidth: 200, "& .MuiSelect-select": { py: "7px !important" } }}>
              <MenuItem value="">انتخابِ پنل</MenuItem>
              {panels.map((p: any) => <MenuItem key={p.id} value={p.id}>{p.key}{p.host_aliases?.length ? "" : " (بدون دامنهٔ قبلی)"}</MenuItem>)}
            </Select>
            {migPreview?.aliases?.length > 1 && (
              <Select size="small" value={migPrevHost} displayEmpty
                onChange={(e) => setMigPrevHost(e.target.value)}
                sx={{ minWidth: 200, "& .MuiSelect-select": { py: "7px !important" } }}>
                {migPreview.aliases.map((h: string) => <MenuItem key={h} value={h}>{h}</MenuItem>)}
              </Select>
            )}
            <Button variant="outlined" startIcon={<GroupIcon />} disabled={!migPanelId || migPrev.isPending}
              onClick={() => migPrev.mutate()}>پیش‌نمایش</Button>
          </Stack>

          {migPreview && (
            <Alert severity={migPreview.previous_host ? "info" : "warning"} sx={{ mb: 2 }}>
              {migPreview.previous_host ? (<>
                به <b>{fmtNum(migPreview.total)}</b> نمایندهٔ ثبت‌شده ارسال می‌شود
                {migPreview.unregistered ? ` • ${fmtNum(migPreview.unregistered)} بدون ربات (پیام دریافت نمی‌کنند)` : ""}.
                <Box sx={{ mt: 1, fontSize: 13 }}>
                  <Box sx={{ mt: 0.5 }}>🟢 نمونهٔ آدرسِ جدید:</Box>
                  <Box component="code" dir="ltr" sx={{ display: "block", wordBreak: "break-all", fontFamily: "monospace", color: "success.main" }}>{migPreview.sample_new_link}</Box>
                  <Box sx={{ mt: 0.5 }}>🔵 نمونهٔ آدرسِ قبلی:</Box>
                  <Box component="code" dir="ltr" sx={{ display: "block", wordBreak: "break-all", fontFamily: "monospace", color: "text.secondary" }}>{migPreview.sample_previous_link}</Box>
                </Box>
              </>) : "این پنل «دامنهٔ قبلی» ندارد؛ ابتدا در صفحهٔ «پنل‌ها» با «انتقال دامنه» یا بخشِ «دامنه‌های قبلی» آن را ثبت کنید."}
            </Alert>
          )}

          <Button variant="contained" color="primary" startIcon={<PublicIcon />}
            disabled={!migPanelId || !migPreview?.previous_host || !migPreview?.total || migSend.isPending}
            onClick={() => {
              if (window.confirm(`پیامِ آدرسِ جدید به ${fmtNum(migPreview.total)} نماینده ارسال شود؟`))
                migSend.mutate();
            }}>ارسالِ اعلام</Button>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <CleaningServicesIcon color="warning" />
            <Typography variant="h6">پاک‌سازی کانال</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            کاربرانی که ربات را آغاز کرده‌اند اما نماینده نیستند و در کانال عضو شده‌اند، حذف می‌شوند.
            تا زمانی که در تنظیمات «مسدودسازی واقعی کانال» را روشن نکنید، تنها حالت آزمایشی (گزارش) اجرا می‌شود.
          </Typography>
          <Button variant="outlined" color="warning" startIcon={<CleaningServicesIcon />}
            disabled={guard.isPending} onClick={() => guard.mutate()}>
            اجرای پاک‌سازی کانال
          </Button>
        </CardContent>
      </Card>

      <Card sx={{ mt: 2 }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <WarningAmberIcon color="warning" />
            <Typography variant="h6">هشدارِ کاربرانِ پرحجم</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            کاربرانی که با حجمِ بسیار بالا (حجمِ پیش‌فرضِ ۱۰۰۰ گیگابایتِ هیدیفای، اغلب اشتباهی) ساخته شده‌اند
            و فاکتورِ نمایندهٔ اصلی را سنگین می‌کنند. می‌توانید به نمایندهٔ اصلیِ هر کاربر هشدار دهید تا حجم را اصلاح کند.
          </Typography>

          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }} useFlexGap flexWrap="wrap" alignItems="center">
            <NumberField size="small" label="آستانهٔ حجم (گیگابایت)" sx={{ minWidth: 150 }}
              value={hvThreshold} placeholder="1000"
              onChange={setHvThreshold} />
            <Select size="small" value={hvPanelId} displayEmpty
              onChange={(e) => { setHvPanelId(e.target.value); setHvData(null); }}
              renderValue={(v) => v ? (panels.find((p: any) => String(p.id) === String(v))?.key ?? v) : "همهٔ پنل‌ها"}
              sx={{ minWidth: 150, "& .MuiSelect-select": { py: "7px !important" } }}>
              <MenuItem value="">همهٔ پنل‌ها</MenuItem>
              {panels.map((p: any) => <MenuItem key={p.id} value={p.id}>{p.key}</MenuItem>)}
            </Select>
            <FormControlLabel control={<Switch checked={hvThisMonth} onChange={(e) => { setHvThisMonth(e.target.checked); setHvData(null); }} />} label="فقط ماهِ جاری" />
            <Button variant="outlined" startIcon={<GroupIcon />} disabled={hvCheck.isPending}
              onClick={() => { setHvDelResult(null); hvCheck.mutate(); }}>بررسی / نمایش</Button>
          </Stack>

          {hvData && (
            <Alert severity={hvRows.length ? "warning" : "success"} sx={{ mb: 2 }}
              action={hvRows.length
                ? <Button color="inherit" size="small" onClick={() => setHvShow((s) => !s)}>{hvShow ? "بستن فهرست" : "نمایش فهرست"}</Button>
                : undefined}>
              {hvRows.length
                ? <><b>{fmtNum(hvRows.length)}</b> کاربر با حجمِ ≥ {fmtNum(hvData.threshold)} گیگابایت پیدا شد.</>
                : `کاربری با حجمِ ≥ ${fmtNum(hvData.threshold)} گیگابایت پیدا نشد.`}
            </Alert>
          )}

          {hvCheck.isPending && <DataState isLoading rows={3}><Box /></DataState>}
          {hvData && hvRows.length > 0 && (
            <Collapse in={hvShow} unmountOnExit>
              <Box sx={{ mb: 2, maxHeight: 360, overflow: "auto", border: 1, borderColor: "divider", borderRadius: 2 }}>
                <Table size="small" stickyHeader className="resp-table">
                  <TableHead>
                    <TableRow>
                      <TableCell>کاربر</TableCell><TableCell>پنل</TableCell>
                      <TableCell>حجم (گیگابایت)</TableCell><TableCell>مبلغِ تقریبی</TableCell>
                      <TableCell>تاریخِ ساخت</TableCell><TableCell>نمایندهٔ اصلی</TableCell>
                      <TableCell align="center">عملیات</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {hvRows.map((r: any, i: number) => {
                      const href = telegramHref(r.root_username, r.root_bot_chat_id);
                      return (
                        <TableRow key={r.snapshot_id ?? `${r.panel_key}-${r.user_uuid}-${i}`} hover>
                          <TableCell>
                            {r.name || "—"}
                            <Typography variant="caption" color="text.secondary" display="block" dir="ltr">{r.user_uuid}</Typography>
                            {r.creator_is_sub && (
                              <Typography variant="caption" color="warning.main" display="block">↳ ساختهٔ زیرمجموعه: {r.creator_name}</Typography>
                            )}
                            {r.from_deleted && (
                              <Typography variant="caption" color="error" display="block">↳ حذف‌شده؛ به‌دلیلِ مصرف، کل حجم محاسبه می‌شود</Typography>
                            )}
                          </TableCell>
                          <TableCell>{r.panel_key}</TableCell>
                          <TableCell dir="ltr">{fmtNum(r.usage_limit_gb)}</TableCell>
                          <TableCell>{fmtToman(r.would_be_toman)}</TableCell>
                          <TableCell dir="ltr">{r.start_date || "—"}</TableCell>
                          <TableCell>
                            {href ? <Link href={href} target="_blank" rel="noopener noreferrer">{r.root_name}</Link> : r.root_name}
                          </TableCell>
                          <TableCell align="center" data-label="">
                            <Stack direction="row" spacing={0.5} justifyContent="center">
                              <Tooltip title={r.root_bot_chat_id ? "هشدار به این نماینده" : "این نماینده در ربات ثبت نشده"}>
                                <span><IconButton size="small" color="warning" disabled={!r.root_bot_chat_id || hvWarnOne.isPending}
                                  onClick={() => hvWarnOne.mutate(r.root_reseller_id)}><NotificationsActiveIcon fontSize="small" /></IconButton></span>
                              </Tooltip>
                              <Tooltip title={r.from_deleted ? "پاک‌سازی از سامانه" : "حذف از پنل و سامانه"}>
                                <span><IconButton size="small" color="error" disabled={hvDelete.isPending}
                                  onClick={() => { setHvDelTargets([r]); setHvDelAck(true); }}>
                                  <DeleteForeverIcon fontSize="small" /></IconButton></span>
                              </Tooltip>
                            </Stack>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </Box>
              {hvDelResult && (
                <Alert sx={{ mb: 2 }}
                  severity={hvDelResult.failed ? "error" : hvDelResult.skipped ? "warning" : "success"}
                  onClose={() => setHvDelResult(null)}>
                  <Typography variant="body2">
                    {fmtNum(hvDelResult.deleted)} کاربر از پنل حذف شد
                    {hvDelResult.already_absent > 0 && <> — {fmtNum(hvDelResult.already_absent)} مورد از قبل روی پنل نبود</>}
                    {" "}— {fmtNum(hvDelResult.purged)} مورد از سابقهٔ سامانه پاک شد
                    {hvDelResult.failed > 0 && <> — {fmtNum(hvDelResult.failed)} ناموفق</>}
                    {hvDelResult.skipped > 0 && <> — {fmtNum(hvDelResult.skipped)} رد شد</>}
                  </Typography>
                  {hvDelResult.purged > 0 && (
                    <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>
                      فاکتورهای صادرشده تغییر نکرده‌اند؛ برای اعمال روی این دوره، «صدور فاکتورهای دوره» را دوباره اجرا کنید.
                    </Typography>
                  )}
                  {hvDelResult.rows.filter((x: HighVolumeDeleteRow) => !x.purged).length > 0 && (
                    <Box sx={{ mt: 1 }}>
                      {hvDelResult.rows.filter((x: HighVolumeDeleteRow) => !x.purged).map((x: HighVolumeDeleteRow) => (
                        <Typography key={x.snapshot_id} variant="caption" display="block">
                          • {x.name || x.user_uuid} — {HV_DEL_FA[x.status] || x.status}
                          {x.error && <Typography component="span" variant="caption" color="text.secondary" dir="ltr"> ({x.error})</Typography>}
                        </Typography>
                      ))}
                    </Box>
                  )}
                </Alert>
              )}
              <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
                <Button variant="contained" color="warning" startIcon={<NotificationsActiveIcon />}
                  disabled={hvWarnAll.isPending}
                  onClick={() => {
                    const admins = new Set(hvRows.map((r: any) => r.root_reseller_id)).size;
                    if (window.confirm(`هشدار به ${fmtNum(admins)} نمایندهٔ اصلی ارسال شود؟`))
                      hvWarnAll.mutate();
                  }}>ارسالِ هشدار به همهٔ نماینده‌های جدول</Button>
                <Button variant="outlined" color="error" startIcon={<DeleteForeverIcon />}
                  disabled={hvDelete.isPending || !hvRows.length}
                  onClick={() => { setHvDelTargets(hvRows); setHvDelAck(false); }}>
                  حذفِ همهٔ کاربرانِ جدول
                </Button>
              </Stack>
            </Collapse>
          )}
        </CardContent>

        <Dialog open={!!hvDelTargets} onClose={() => hvDelete.isPending || setHvDelTargets(null)}
          fullWidth maxWidth="xs">
          {hvDelTargets && (() => {
            const one = hvDelTargets.length === 1 ? hvDelTargets[0] : null;
            const byPanel = hvDelTargets.reduce((m: Record<string, number>, r: any) => {
              m[r.panel_key] = (m[r.panel_key] || 0) + 1; return m;
            }, {});
            return (
              <>
                <DialogTitle>
                  {one ? `حذفِ کاربر — ${one.name || one.user_uuid}` : `حذفِ ${fmtNum(hvDelTargets.length)} کاربرِ جدول`}
                </DialogTitle>
                <DialogContent>
                  {one ? (
                    <Typography variant="body2" sx={{ mb: 1 }}>
                      کاربر با سهمیهٔ <b>{fmtNum(one.usage_limit_gb)}</b> گیگابایت روی پنلِ <b>{one.panel_key}</b>،
                      نمایندهٔ <b>{one.root_name}</b>.
                    </Typography>
                  ) : (
                    <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap" sx={{ mb: 1 }}>
                      {Object.entries(byPanel).map(([k, n]) => (
                        <Chip key={k} size="small" label={`${k}: ${fmtNum(n as number)}`} />
                      ))}
                    </Stack>
                  )}
                  <Typography variant="body2" sx={{ mb: 1 }}>
                    {one?.from_deleted
                      ? "این کاربر روی پنل موجود نیست؛ فقط از سابقهٔ سامانه پاک می‌شود تا در فاکتور محاسبه نشود."
                      : "ابتدا از پنلِ هیدیفای حذف می‌شود و تنها در صورتِ موفقیت، از سابقهٔ سامانه پاک می‌شود (هم سهمیه و هم مصرف/متره). اگر حذف از پنل انجام نشود، هیچ چیزی پاک نمی‌شود."}
                  </Typography>
                  <Typography variant="body2" color="error" sx={{ fontWeight: 700, mb: 1 }}>
                    ⚠️ این عملیات برگشت‌ناپذیر است؛ کاربر روی پنل حذف می‌شود.
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    فاکتورهای صادرشده تغییر نمی‌کنند؛ برای اعمال روی این دوره، «صدور فاکتورهای دوره» را دوباره اجرا کنید.
                  </Typography>
                  {!one && (
                    <FormControlLabel sx={{ mt: 1 }}
                      control={<Checkbox checked={hvDelAck} onChange={(e) => setHvDelAck(e.target.checked)} />}
                      label="می‌دانم این عملیات برگشت‌ناپذیر است" />
                  )}
                </DialogContent>
                <DialogActions>
                  <Button disabled={hvDelete.isPending} onClick={() => setHvDelTargets(null)}>انصراف</Button>
                  <Button variant="contained" color="error" disabled={!hvDelAck || hvDelete.isPending}
                    startIcon={hvDelete.isPending ? <CircularProgress size={18} color="inherit" /> : <DeleteForeverIcon />}
                    onClick={() => hvDelete.mutate(hvDelTargets)}>حذف</Button>
                </DialogActions>
              </>
            );
          })()}
        </Dialog>
      </Card>
      {node}
    </Box>
  );
}
