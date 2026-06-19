import { useState } from "react";
import {
  Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  Grid, LinearProgress, Stack, TextField, Typography,
} from "@mui/material";
import BlockIcon from "@mui/icons-material/esm/Block";
import LockOpenIcon from "@mui/icons-material/esm/LockOpen";
import SpeedIcon from "@mui/icons-material/esm/Speed";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  portalSubs, PortalSub, portalSetSubCap, portalSuspendSub, portalRestoreSub,
} from "../portalClient";
import { DataState } from "../../components/DataState";
import { useToast, errMsg } from "../../components/Toast";
import { fmtGb, fmtNum, fmtToman } from "../../format";
import { EmptyState } from "../ui";

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

export default function PortalSubs() {
  const qc = useQueryClient();
  const { node: toast, show } = useToast();
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-subs"],
    queryFn: portalSubs,
  });
  const [capSub, setCapSub] = useState<PortalSub | null>(null);
  const [capValue, setCapValue] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = () => qc.invalidateQueries({ queryKey: ["portal-subs"] });

  const openCap = (sub: PortalSub) => {
    setCapSub(sub);
    setCapValue(sub.gb_cap ? String(sub.gb_cap) : "");
  };

  const saveCap = async () => {
    if (!capSub) return;
    const gb = parseInt(capValue || "0", 10);
    if (isNaN(gb) || gb < 0) { show("عدد نامعتبر است", "warning"); return; }
    setBusy(true);
    try {
      await portalSetSubCap(capSub.id, gb);
      show(gb > 0 ? `سقف «${capSub.name}» روی ${gb} گیگ تنظیم شد` : `سقف «${capSub.name}» حذف شد`, "success");
      setCapSub(null);
      refresh();
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  const toggleEnforce = async (sub: PortalSub) => {
    const enforced = sub.enforcement_state === "enforced";
    setBusy(true);
    try {
      const r = enforced ? await portalRestoreSub(sub.id) : await portalSuspendSub(sub.id);
      if (r.error) show(`عملیات با خطا: ${r.error}`, "error");
      else show(enforced ? `آزادسازی «${sub.name}» در صف قرار گرفت` : `مسدودسازی «${sub.name}» در صف قرار گرفت`, "success");
      refresh();
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  return (
    <Box>
      {toast}
      <Typography variant="h5" sx={{ mb: 0.4 }}>زیرمجموعه‌ها</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        زیرمجموعه‌های مستقیمِ شما، حجمِ فروشِ این ماه و سقفِ حجمِ هرکدام
      </Typography>

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch} rows={4}>
        {data.length === 0 ? (
          <Card><EmptyState>زیرمجموعه‌ای ندارید.</EmptyState></Card>
        ) : (
          <Grid container spacing={2}>
            {data.map((sub) => {
              const enforced = sub.enforcement_state === "enforced";
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
                          label={enforced ? "مسدود" : "فعال"}
                          color={enforced ? "error" : "success"}
                          variant={enforced ? "filled" : "outlined"}
                        />
                      </Stack>

                      <Stack direction="row" spacing={1} sx={{ mt: 1.5, mb: 1.5 }}>
                        <Box sx={{ flex: 1, p: 1, borderRadius: 2, bgcolor: "action.hover", textAlign: "center" }}>
                          <Typography variant="caption" color="text.secondary">کاربران</Typography>
                          <Typography sx={{ fontWeight: 800 }}>{fmtNum(sub.enabled_users)}/{fmtNum(sub.users)}</Typography>
                        </Box>
                        <Box sx={{ flex: 1, p: 1, borderRadius: 2, bgcolor: "action.hover", textAlign: "center" }}>
                          <Typography variant="caption" color="text.secondary">فروشِ این ماه</Typography>
                          <Typography sx={{ fontWeight: 800, fontSize: 14, whiteSpace: "nowrap" }}>{fmtToman(sub.this_month_amount)}</Typography>
                        </Box>
                      </Stack>

                      <CapMeter sub={sub} />

                      <Stack direction="row" spacing={1} sx={{ mt: 1.8 }}>
                        <Button
                          size="small"
                          variant="outlined"
                          startIcon={<SpeedIcon sx={{ fontSize: 17 }} />}
                          onClick={() => openCap(sub)}
                          disabled={busy}
                          sx={{ flex: 1 }}
                        >
                          سقف حجم
                        </Button>
                        <Button
                          size="small"
                          variant="outlined"
                          color={enforced ? "success" : "error"}
                          startIcon={enforced ? <LockOpenIcon sx={{ fontSize: 17 }} /> : <BlockIcon sx={{ fontSize: 17 }} />}
                          onClick={() => toggleEnforce(sub)}
                          disabled={busy}
                          sx={{ flex: 1 }}
                        >
                          {enforced ? "آزادسازی" : "مسدودسازی"}
                        </Button>
                      </Stack>
                    </CardContent>
                  </Card>
                </Grid>
              );
            })}
          </Grid>
        )}
      </DataState>

      <Dialog open={!!capSub} onClose={busy ? undefined : () => setCapSub(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontWeight: 800 }}>سقف حجم ماهانه — {capSub?.name}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            سقفِ حجمِ ماهانه (گیگابایت). برای حذفِ سقف عدد 0 را وارد کنید. این سقف فقط برای هشدار است و
            مسدودسازی خودکار نمی‌کند.
          </Typography>
          <TextField
            autoFocus
            fullWidth
            type="number"
            label="سقف (گیگ)"
            value={capValue}
            onChange={(e) => setCapValue(e.target.value)}
            inputProps={{ min: 0, dir: "ltr" }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCapSub(null)} disabled={busy}>انصراف</Button>
          <Button variant="contained" onClick={saveCap} disabled={busy}>ذخیره</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
