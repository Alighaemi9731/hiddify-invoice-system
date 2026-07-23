import { useState } from "react";
import {
  Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, Grid, LinearProgress, Menu, MenuItem, Skeleton, Stack, Switch, TextField, Typography,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import BlockIcon from "@mui/icons-material/esm/Block";
import LockOpenIcon from "@mui/icons-material/esm/LockOpen";
import PersonAddDisabledIcon from "@mui/icons-material/esm/PersonAddDisabled";
import SpeedIcon from "@mui/icons-material/esm/Speed";
import AddCircleOutlineIcon from "@mui/icons-material/esm/AddCircleOutline";
import PictureAsPdfIcon from "@mui/icons-material/esm/PictureAsPdf";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  portalSubs, PortalSub, portalSetSubCap, portalSuspendSub, portalFreezeSub, portalRestoreSub,
  portalBumpSub, portalSubCanAddAdmin, openPortalSubPdf, portalSubSalesByDay,
} from "../portalClient";
import { DataState } from "../../components/DataState";
import { useToast, errMsg } from "../../components/Toast";
import CapacityBar from "../../components/CapacityBar";
import EChart from "../../components/EChart";
import { dailyTrendOption } from "../dailyTrend";
import { fmtGb, fmtNum, fmtToman } from "../../format";
import { EmptyState } from "../ui";
import { useXsFullScreen } from "../../responsive";

const BUMP_CHIPS = [50, 100, 200, 500];

function CapMeter({ sub }: { sub: PortalSub }) {
  if (!sub.gb_cap) {
    return <Typography variant="caption" color="text.disabled">بدون سقفِ حجمِ ماهانه</Typography>;
  }
  const pct = Math.min(100, Math.round(sub.cap_pct ?? 0));
  const color: "info" | "warning" | "error" = pct >= 90 ? "error" : pct >= 70 ? "warning" : "info";
  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.3 }}>
        <Typography variant="caption" color="text.secondary">
          {fmtGb(sub.current_gb)} از {fmtGb(sub.gb_cap)}
        </Typography>
        <Typography variant="caption" color={`${color}.main`}>{fmtNum(pct)}٪</Typography>
      </Stack>
      <LinearProgress variant="determinate" value={pct} color={color} sx={{ height: 7, borderRadius: 3 }} />
    </Box>
  );
}

// Current-month daily sale trend per sub — same look as the owner dashboard's «روند فروش روزانه».
// Lazily fetched per card (React Query dedupes/caches).
function SubDailyChart({ subId }: { subId: number }) {
  const theme = useTheme();
  const { data = [], isLoading } = useQuery({
    queryKey: ["portal-sub-salesbyday", subId],
    queryFn: () => portalSubSalesByDay(subId),
    staleTime: 300000,
  });
  const empty = !data.length || data.every((d) => !d.amount_toman);
  return (
    <Box sx={{ mt: 1.5 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
        روندِ فروشِ روزانه (ماهِ جاری)
      </Typography>
      {isLoading ? (
        <Skeleton variant="rounded" height={120} />
      ) : empty ? (
        <Typography variant="caption" color="text.disabled" sx={{ display: "block", py: 2, textAlign: "center" }}>
          فروشی این ماه ثبت نشده است.
        </Typography>
      ) : (
        <EChart option={dailyTrendOption(theme, data, { compact: true })} height={120}
          ariaLabel="روند فروش روزانهٔ زیرمجموعه" />
      )}
    </Box>
  );
}

export default function PortalSubs() {
  const xsFull = useXsFullScreen();
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-subs"],
    queryFn: portalSubs,
  });
  const [capSub, setCapSub] = useState<PortalSub | null>(null);
  const [capValue, setCapValue] = useState("");
  const [bumpSub, setBumpSub] = useState<PortalSub | null>(null);
  const [bumpValue, setBumpValue] = useState("");
  const [pdfMenu, setPdfMenu] = useState<{ el: HTMLElement; sub: PortalSub } | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () => qc.invalidateQueries({ queryKey: ["portal-subs"] });

  const openCap = (sub: PortalSub) => { setCapSub(sub); setCapValue(sub.gb_cap ? String(sub.gb_cap) : ""); };
  const openBump = (sub: PortalSub) => { setBumpSub(sub); setBumpValue(""); };

  const saveCap = async () => {
    if (!capSub) return;
    const gb = parseInt(capValue || "0", 10);
    if (isNaN(gb) || gb < 0) { show("عددِ واردشده معتبر نیست", "warning"); return; }
    setBusy(true);
    try {
      await portalSetSubCap(capSub.id, gb);
      show(gb > 0 ? `سقف «${capSub.name}» روی ${gb} گیگابایت تنظیم شد` : `سقف «${capSub.name}» حذف شد`, "success");
      setCapSub(null); refresh();
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  const saveBump = async () => {
    if (!bumpSub) return;
    const amount = parseInt(bumpValue || "0", 10);
    if (isNaN(amount) || amount <= 0 || amount > 5000) { show("مقدار باید بین ۱ تا ۵۰۰۰ باشد", "warning"); return; }
    setBusy(true);
    try {
      const r = await portalBumpSub(bumpSub.id, amount);
      show(`ظرفیتِ «${bumpSub.name}» به ${fmtNum(r.max_users)} رسید`, "success");
      setBumpSub(null); refresh();
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  const toggleCanAddAdmin = async (sub: PortalSub) => {
    setBusy(true);
    try {
      await portalSubCanAddAdmin(sub.id, !sub.can_add_admin);
      show(!sub.can_add_admin ? "اجازهٔ ساختِ زیرمجموعه فعال شد" : "اجازهٔ ساختِ زیرمجموعه غیرفعال شد", "success");
      refresh();
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  const runSubAction = async (
    sub: PortalSub,
    fn: (id: number) => Promise<{ status: string; error: string | null }>,
    okMsg: string,
  ) => {
    setBusy(true);
    try {
      const r = await fn(sub.id);
      if (r.error) show(`خطا در انجامِ عملیات: ${r.error}`, "error");
      else show(okMsg, "success");
      refresh();
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  const downloadPdf = async (sub: PortalSub, period: string) => {
    setPdfMenu(null);
    try { await openPortalSubPdf(sub.id, period); }
    catch (e: any) {
      // 404 → no sales that month
      show(e?.response?.status === 404 ? "برای این دوره فروشی ثبت نشده است" : errMsg(e), "warning");
    }
  };

  return (
    <Box>
      {toast}
      <Typography variant="h5" sx={{ mb: 0.4 }}>زیرمجموعه‌ها</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        زیرمجموعه‌های مستقیمِ شما — ظرفیت، فروش، سقفِ حجم و مدیریتِ هرکدام
      </Typography>

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch} rows={4}>
        {data.length === 0 ? (
          <Card><EmptyState>زیرمجموعه‌ای ندارید.</EmptyState></Card>
        ) : (
          <Grid container spacing={2}>
            {data.map((sub) => {
              const enforced = sub.enforcement_state === "enforced";
              const frozen = sub.enforcement_state === "frozen";
              return (
                <Grid item xs={12} sm={6} lg={4} key={sub.id}>
                  <Card sx={{ height: "100%" }}>
                    <CardContent>
                      <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={1}>
                        <Box sx={{ minWidth: 0 }}>
                          <Typography variant="subtitle1" noWrap sx={{ fontWeight: 800 }}>{sub.name}</Typography>
                          <Typography variant="caption" color="text.secondary">
                            {sub.panel_key} · زیرمجموعهٔ {sub.parent_name}
                          </Typography>
                        </Box>
                        <Chip
                          size="small"
                          label={enforced ? "مسدود" : frozen ? "محدود" : "فعال"}
                          color={enforced ? "error" : frozen ? "warning" : "success"}
                          variant={enforced || frozen ? "filled" : "outlined"}
                        />
                      </Stack>

                      <Box sx={{ mt: 1.5 }}>
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.3 }}>
                          ظرفیتِ کاربران (ساخته‌شده / سقف)
                        </Typography>
                        <CapacityBar used={sub.users} max={sub.max_users} />
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                          {fmtNum(sub.enabled_users)} کاربرِ فعال · فروشِ این ماه: {fmtToman(sub.this_month_amount)}
                        </Typography>
                      </Box>

                      <SubDailyChart subId={sub.id} />

                      <Box sx={{ mt: 1.5 }}><CapMeter sub={sub} /></Box>

                      <FormControlLabel
                        sx={{ mt: 1, ml: 0 }}
                        control={
                          <Switch
                            size="small"
                            checked={sub.can_add_admin}
                            onChange={() => toggleCanAddAdmin(sub)}
                            disabled={busy}
                          />
                        }
                        label={<Typography variant="caption">اجازهٔ ساختِ زیرمجموعه</Typography>}
                      />

                      <Stack direction="row" spacing={1} sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
                        <Button size="small" variant="outlined" startIcon={<SpeedIcon sx={{ fontSize: 17 }} />}
                          onClick={() => openCap(sub)} disabled={busy}>سقف حجم</Button>
                        <Button size="small" variant="outlined" startIcon={<AddCircleOutlineIcon sx={{ fontSize: 17 }} />}
                          onClick={() => openBump(sub)} disabled={busy}>افزایش ظرفیت</Button>
                        <Button size="small" variant="outlined" startIcon={<PictureAsPdfIcon sx={{ fontSize: 17 }} />}
                          onClick={(e) => setPdfMenu({ el: e.currentTarget, sub })} disabled={busy}>PDF</Button>
                        {enforced ? (
                          <Button size="small" variant="outlined" color="success"
                            startIcon={<LockOpenIcon sx={{ fontSize: 17 }} />}
                            onClick={() => runSubAction(sub, portalRestoreSub, `آزادسازی «${sub.name}» در صف قرار گرفت`)}
                            disabled={busy}>آزادسازی</Button>
                        ) : frozen ? (
                          <>
                            <Button size="small" variant="outlined" color="success"
                              startIcon={<LockOpenIcon sx={{ fontSize: 17 }} />}
                              onClick={() => runSubAction(sub, portalRestoreSub, `رفع توقف ساخت کاربر «${sub.name}» در صف قرار گرفت`)}
                              disabled={busy}>رفع توقف ساخت کاربر</Button>
                            <Button size="small" variant="outlined" color="error"
                              startIcon={<BlockIcon sx={{ fontSize: 17 }} />}
                              onClick={() => runSubAction(sub, portalSuspendSub, `مسدودسازی «${sub.name}» در صف قرار گرفت`)}
                              disabled={busy}>مسدودسازی کامل</Button>
                          </>
                        ) : (
                          <>
                            <Button size="small" variant="outlined" color="warning"
                              startIcon={<PersonAddDisabledIcon sx={{ fontSize: 17 }} />}
                              onClick={() => runSubAction(sub, portalFreezeSub, `توقف ساخت کاربر برای «${sub.name}» در صف قرار گرفت`)}
                              disabled={busy}>توقف ساخت کاربر</Button>
                            <Button size="small" variant="outlined" color="error"
                              startIcon={<BlockIcon sx={{ fontSize: 17 }} />}
                              onClick={() => runSubAction(sub, portalSuspendSub, `مسدودسازی «${sub.name}» در صف قرار گرفت`)}
                              disabled={busy}>مسدودسازی</Button>
                          </>
                        )}
                      </Stack>
                    </CardContent>
                  </Card>
                </Grid>
              );
            })}
          </Grid>
        )}
      </DataState>

      {/* PDF month picker */}
      <Menu anchorEl={pdfMenu?.el} open={!!pdfMenu} onClose={() => setPdfMenu(null)}>
        {(pdfMenu?.sub.months || []).map((m) => (
          <MenuItem key={m.label} onClick={() => pdfMenu && downloadPdf(pdfMenu.sub, m.label)}>
            <span dir="ltr" style={{ fontFamily: "monospace", marginInlineEnd: 8 }}>{m.label}</span>
            — {fmtToman(m.amount_toman)}
          </MenuItem>
        ))}
      </Menu>

      {/* GB cap dialog */}
      <Dialog open={!!capSub} onClose={busy ? undefined : () => setCapSub(null)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <DialogTitle sx={{ fontWeight: 800 }}>سقف حجم ماهانه — {capSub?.name}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            سقفِ حجمِ ماهانه (گیگابایت). برای حذفِ سقف، عدد ۰ را وارد کنید. این سقف تنها برای هشدار است و
            مسدودسازیِ خودکار در پی ندارد.
          </Typography>
          <TextField autoFocus fullWidth type="number" label="سقف (گیگابایت)" value={capValue}
            onChange={(e) => setCapValue(e.target.value)} inputProps={{ min: 0, dir: "ltr" }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCapSub(null)} disabled={busy}>انصراف</Button>
          <Button variant="contained" onClick={saveCap} disabled={busy}>ذخیره</Button>
        </DialogActions>
      </Dialog>

      {/* Bump capacity dialog */}
      <Dialog open={!!bumpSub} onClose={busy ? undefined : () => setBumpSub(null)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <DialogTitle sx={{ fontWeight: 800 }}>افزایش ظرفیت — {bumpSub?.name}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            مقدارِ افزودنی به سقفِ کاربرانِ این زیرمجموعه را انتخاب یا وارد کنید (به سقفِ کاربران و
            کاربرانِ فعال افزوده می‌شود).
          </Typography>
          <Stack direction="row" spacing={1} sx={{ mb: 1.5 }} flexWrap="wrap" useFlexGap>
            {BUMP_CHIPS.map((n) => (
              <Chip key={n} label={`+${fmtNum(n)}`} onClick={() => setBumpValue(String(n))}
                color={bumpValue === String(n) ? "primary" : "default"}
                variant={bumpValue === String(n) ? "filled" : "outlined"} />
            ))}
          </Stack>
          <TextField fullWidth type="number" label="مقدار افزایش" value={bumpValue}
            onChange={(e) => setBumpValue(e.target.value)} inputProps={{ min: 1, max: 5000, dir: "ltr" }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setBumpSub(null)} disabled={busy}>انصراف</Button>
          <Button variant="contained" onClick={saveBump} disabled={busy}>اعمال</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
