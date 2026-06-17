import { useEffect, useMemo, useState } from "react";
import {
  Box, Card, CardContent, Typography, TextField, Button, Stack, Alert, MenuItem, Select,
  Chip, Divider, Table, TableBody, TableCell, TableHead, TableRow, Collapse,
} from "@mui/material";
import CampaignIcon from "@mui/icons-material/esm/Campaign";
import CleaningServicesIcon from "@mui/icons-material/esm/CleaningServices";
import GroupIcon from "@mui/icons-material/esm/Group";
import { useMutation, useQuery } from "@tanstack/react-query";
import { broadcastMessage, broadcastPreview, broadcastStatus, runChannelGuard, listPanels } from "../api/client";
import { useToast, errMsg } from "../components/Toast";
import { fmtNum } from "../format";

// Audience filters — each is applied ON TOP of the base set (resellers in the main «نمایندگان»
// list that are not exempt from billing and are on an active panel). The panel filter is combinable.
const AUDIENCES: { value: string; label: string; threshold?: { label: string; def: number } }[] = [
  { value: "all", label: "همه نمایندگان" },
  { value: "debtors", label: "بدهکاران (فاکتور پرداخت‌نشده)" },
  { value: "zero_sale", label: "فروش صفرِ این ماه" },
  { value: "few_active", label: "کم‌تر از N کاربرِ فعال", threshold: { label: "کم‌تر از این تعداد کاربرِ فعال", def: 10 } },
  { value: "invoice_below", label: "فاکتورِ این ماه زیرِ مبلغ", threshold: { label: "فاکتور کم‌تر از این مبلغ (تومان)", def: 100000 } },
];

const STATUS_FA: Record<string, string> = {
  sent: "ارسال شد", blocked: "مسدود کرده", failed: "ناموفق", unregistered: "بدون ربات", pending: "—",
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
        + (r.unregistered ? ` (${fmtNum(r.unregistered)} بدون ربات پیام نمی‌گیرند)` : "")
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
            فیلتر و پنل را انتخاب کنید، «پیش‌نمایشِ گیرندگان» را بزنید تا دقیقاً ببینید پیام به چه کسانی می‌رود، بعد ارسال کنید.
            ارسال در <b>پس‌زمینه</b> انجام می‌شود؛ پیشرفتِ زنده همین‌جا نمایش داده می‌شود و <b>خلاصهٔ نهایی به تلگرامِ شما</b> می‌رسد.
          </Typography>

          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }} useFlexGap flexWrap="wrap">
            <Select size="small" value={audience}
              onChange={(e) => onFilterChange(() => setAudience(e.target.value))}
              sx={{ minWidth: 240, "& .MuiSelect-select": { py: "7px !important" } }}>
              {AUDIENCES.map((a) => <MenuItem key={a.value} value={a.value}>{a.label}</MenuItem>)}
            </Select>
            <Select size="small" value={panelId} displayEmpty
              onChange={(e) => onFilterChange(() => setPanelId(e.target.value))}
              renderValue={(v) => v ? (panels.find((p: any) => String(p.id) === String(v))?.key ?? v) : "همهٔ پنل‌ها"}
              sx={{ minWidth: 160, "& .MuiSelect-select": { py: "7px !important" } }}>
              <MenuItem value="">همهٔ پنل‌ها</MenuItem>
              {panels.map((p: any) => <MenuItem key={p.id} value={p.id}>{p.key}</MenuItem>)}
            </Select>
            {needsThreshold && (
              <TextField size="small" type="number" sx={{ minWidth: 220 }}
                label={audDef!.threshold!.label}
                value={threshold} placeholder={String(audDef!.threshold!.def)}
                onChange={(e) => onFilterChange(() => setThreshold(e.target.value))}
                InputProps={{ inputProps: { min: 0, dir: "ltr" } }} />
            )}
            <Button variant="outlined" startIcon={<GroupIcon />} disabled={preview.isPending}
              onClick={() => preview.mutate()}>پیش‌نمایشِ گیرندگان</Button>
          </Stack>

          {report && (
            <Alert severity="info" sx={{ mb: 2 }}
              action={rows.length
                ? <Button color="inherit" size="small" onClick={() => setShowList((s) => !s)}>{showList ? "بستن لیست" : "نمایش لیست"}</Button>
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

          {report && (
            <Collapse in={showList} unmountOnExit>
              <Box sx={{ mb: 2, maxHeight: 320, overflow: "auto", border: 1, borderColor: "divider", borderRadius: 2 }}>
                <Table size="small" stickyHeader>
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

      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <CleaningServicesIcon color="warning" />
            <Typography variant="h6">پاک‌سازی کانال</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            کاربرانی که ربات را استارت زده‌اند ولی نماینده نیستند و در کانال عضو شده‌اند، حذف می‌شوند.
            تا وقتی در تنظیمات «مسدودسازی واقعی کانال» را روشن نکنید، فقط حالت آزمایشی (گزارش) اجرا می‌شود.
          </Typography>
          <Button variant="outlined" color="warning" startIcon={<CleaningServicesIcon />}
            disabled={guard.isPending} onClick={() => guard.mutate()}>
            اجرای پاک‌سازی کانال
          </Button>
        </CardContent>
      </Card>
      {node}
    </Box>
  );
}
