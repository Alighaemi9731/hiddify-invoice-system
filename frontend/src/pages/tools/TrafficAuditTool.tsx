import { useEffect, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, LinearProgress, Stack, Table, TableBody,
  TableCell, TableHead, TableRow, Tooltip, Typography, alpha,
} from "@mui/material";
import TravelExploreIcon from "@mui/icons-material/esm/TravelExplore";
import ReportProblemIcon from "@mui/icons-material/esm/ReportProblem";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  trafficAuditLatest, trafficAuditRun, trafficAuditStatus,
  TrafficAuditReport, TrafficAuditRow,
} from "../../api/client";
import { DataState } from "../../components/DataState";
import { errMsg } from "../../components/Toast";
import { fmtGb, fmtNum, fmtDate } from "../../format";

const fa = (n: number, digits = 1) =>
  n.toLocaleString("fa-IR", { maximumFractionDigits: digits });

/** «—» rather than a number we never measured. A 0 here would read as "they used nothing". */
const dash = (v: number | null | undefined, render: (n: number) => string) =>
  v === null || v === undefined ? "—" : render(v);

function RatioCell({ row }: { row: TrafficAuditRow }) {
  if (row.ratio === null) {
    // No quota sold at all, yet traffic moved — an infinite ratio, and the state an abuser ends
    // in after deleting the configs. Never render this as "no data".
    return row.flagged
      ? <Chip size="small" color="error" label="حجمی نفروخته" />
      : <Typography variant="body2" color="text.secondary">—</Typography>;
  }
  const label = `${fa(row.ratio)}×`;
  return row.flagged
    ? <Chip size="small" color="error" label={label} />
    : <Typography variant="body2">{label}</Typography>;
}

export default function TrafficAuditTool() {
  const [live, setLive] = useState<TrafficAuditReport | null>(null);
  const [scanning, setScanning] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);

  // What the daily automatic scan stored. Shown on open so the card is never empty, and the only
  // window onto the scheduled run (it happens in a different container).
  const stored = useQuery({ queryKey: ["traffic-audit-latest"], queryFn: trafficAuditLatest });

  const status = useQuery({
    queryKey: ["traffic-audit-status"],
    queryFn: trafficAuditStatus,
    enabled: scanning,
    refetchInterval: 2000,
  });

  useEffect(() => {
    if (scanning && status.data && !status.data.running) {
      setScanning(false);
      if (status.data.result) setLive(status.data.result);
      stored.refetch();
    }
  }, [scanning, status.data]);

  const start = useMutation({
    mutationFn: () => trafficAuditRun(),
    onSuccess: () => { setScanning(true); setStartedAt(Date.now()); setLive(null); },
  });

  const done = status.data?.scanned ?? 0;
  const total = status.data?.total ?? 0;
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  // Only once a few resellers are in, or the first estimate is absurd.
  const eta = (() => {
    if (!startedAt || done < 5 || total <= done) return null;
    const perItem = (Date.now() - startedAt) / done;
    const secs = Math.round((perItem * (total - done)) / 1000);
    return secs > 60 ? `حدوداً ${fa(Math.round(secs / 60), 0)} دقیقه باقی مانده`
                     : `حدوداً ${fa(secs, 0)} ثانیه باقی مانده`;
  })();

  const report = live ?? stored.data ?? null;
  const rows = report?.rows ?? [];
  const flagged = report?.flagged ?? 0;
  const worst = rows.find((r) => r.flagged);

  return (
    <Card>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
          <TravelExploreIcon color="warning" />
          <Typography variant="h6">بررسی مصرف غیرعادی نماینده‌ها</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          مصرفِ واقعیِ هر نمایندهٔ سطح‌اول را از دفترِ ترافیکِ خودِ پنل می‌خواند و با حجمی که
          فروخته مقایسه می‌کند. چون از شمارندهٔ کاربران خوانده نمی‌شود، ریست‌کردنِ شمارنده آن را
          پنهان نمی‌کند. مصرفِ زیرمجموعه‌ها در نمایندهٔ بالادستی حساب می‌شود؛ مالک و نماینده‌های
          معاف از فاکتور در این بررسی نمی‌آیند. <b>هیچ اقدامی انجام نمی‌شود — فقط گزارش.</b>
        </Typography>

        <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
          <Button variant="contained" onClick={() => start.mutate()} disabled={scanning}>
            {scanning ? "در حال بررسی…" : "بررسی زنده"}
          </Button>
          <Button variant="outlined" onClick={() => { setLive(null); stored.refetch(); }}
                  disabled={scanning}>
            آخرین نتیجهٔ ذخیره‌شده
          </Button>
        </Stack>

        {/* A 3-minute shimmer would be a lie — show real progress instead. */}
        {scanning && (
          <Box sx={{ mb: 2 }}>
            <LinearProgress variant={total > 0 ? "determinate" : "indeterminate"} value={pct} />
            <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: "block" }}>
              {total > 0
                ? `${fmtNum(done)} از ${fmtNum(total)} نماینده بررسی شد`
                : "در حال آماده‌سازی…"}
              {status.data?.panel ? ` — پنل ${status.data.panel}` : ""}
              {eta ? ` — ${eta}` : ""}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              بستنِ این صفحه بررسی را متوقف نمی‌کند؛ نتیجه ذخیره می‌شود.
            </Typography>
          </Box>
        )}

        {start.isError && <Alert severity="error" sx={{ mb: 2 }}>{errMsg(start.error)}</Alert>}
        {status.data?.error && (
          <Alert severity="error" sx={{ mb: 2 }}>{status.data.error}</Alert>
        )}

        <DataState isLoading={stored.isLoading && !live} isError={stored.isError}
                   rows={4} onRetry={() => stored.refetch()}>
          <>
            {report && (
              <Alert severity={flagged > 0 ? "error" : "success"} sx={{ mb: 2 }}>
                {flagged > 0
                  ? `${fmtNum(flagged)} نماینده مصرفِ غیرعادی دارد` +
                    (worst?.ratio ? ` — بیشترین: ${worst.reseller_name} با ${fa(worst.ratio)} برابرِ حجمِ فروخته‌شده` : "")
                  : "هیچ نمایندهٔ غیرعادی‌ای پیدا نشد."}
                {report.day ? ` (بررسیِ ${fmtDate(report.day)})` : ""}
              </Alert>
            )}

            {(report?.skipped_panels_detail?.length ?? 0) > 0 && (
              <Alert severity="warning" sx={{ mb: 2 }}>
                این پنل‌ها بررسی نشدند:{" "}
                {report!.skipped_panels_detail!.map((p) => `${p.panel_key} (${p.reason})`).join("، ")}
              </Alert>
            )}

            <Box sx={{ overflowX: "auto", maxHeight: 460 }}>
              <Table size="small" stickyHeader className="resp-table">
                <TableHead>
                  <TableRow>
                    <TableCell>نماینده</TableCell>
                    <TableCell>مصرف ۳۰ روزه</TableCell>
                    <TableCell>حجم فروخته‌شده</TableCell>
                    <TableCell>نسبت</TableCell>
                    <TableCell>مصرف شمارنده‌ها</TableCell>
                    <TableCell>مصرف دیروز</TableCell>
                    <TableCell>کاربر آنلاین دیروز</TableCell>
                    <TableCell>گیگ بر کاربر در روز</TableCell>
                    <TableCell>سهم پنل</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {rows.map((r) => (
                    <TableRow
                      key={`${r.panel_key}-${r.admin_uuid}`}
                      hover
                      sx={r.flagged
                        ? { bgcolor: (t) => alpha(t.palette.error.main, 0.06) }
                        : undefined}
                    >
                      <TableCell>
                        <Stack direction="row" spacing={0.5} alignItems="center">
                          {r.flagged && <ReportProblemIcon color="error" fontSize="small" />}
                          <Box>
                            <Typography variant="body2">{r.reseller_name}</Typography>
                            <Typography variant="caption" color="text.secondary" dir="ltr">
                              {r.panel_key} · {r.admin_uuid}
                              {r.sub_count > 0 ? ` · +${r.sub_count}` : ""}
                            </Typography>
                          </Box>
                        </Stack>
                      </TableCell>
                      <TableCell>{fmtGb(r.last_30d_gb)}</TableCell>
                      <TableCell>
                        <Tooltip title="بیشترِ «حجمِ زندهٔ فعلی» و «حجمِ فروخته‌شده در این ماه». کاربری که ۳۰ گیگ خریده نمی‌تواند بیشتر مصرف کند، پس این سقفِ مصرفِ درست است.">
                          <span>{fmtGb(r.quota_gb)}</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell><RatioCell row={r} /></TableCell>
                      <TableCell>
                        <Typography variant="body2">{fmtGb(r.counter_gb)}</Typography>
                        {r.counter_ratio !== null && r.counter_ratio >= 2 && (
                          <Typography variant="caption" color="error">
                            {fa(r.counter_ratio)}× کمتر از مصرف واقعی
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell>{fmtGb(r.yesterday_gb)}</TableCell>
                      <TableCell>{fmtNum(r.yesterday_online)}</TableCell>
                      <TableCell>{dash(r.gb_per_user_day, (n) => fa(n))}</TableCell>
                      <TableCell>{dash(r.panel_share_pct, (n) => `${fa(n)}٪`)}</TableCell>
                    </TableRow>
                  ))}
                  {rows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={9} align="center"
                                 sx={{ color: "text.secondary", py: 3 }}>
                        هنوز بررسی‌ای انجام نشده است.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </Box>
          </>
        </DataState>
      </CardContent>
    </Card>
  );
}
