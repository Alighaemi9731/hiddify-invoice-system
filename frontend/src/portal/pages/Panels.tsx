import { useState } from "react";
import {
  Box, Button, Card, CardContent, Dialog, DialogActions, DialogContent, DialogTitle,
  Grid, IconButton, Stack, TextField, Tooltip, Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/esm/ContentCopy";
import OpenInNewIcon from "@mui/icons-material/esm/OpenInNew";
import DnsIcon from "@mui/icons-material/esm/Dns";
import TrendingUpIcon from "@mui/icons-material/esm/TrendingUp";
import { useQuery } from "@tanstack/react-query";
import { portalPanels, portalCapacity, portalRequestCapacity, PortalCapacity } from "../portalClient";
import { DataState } from "../../components/DataState";
import { useToast, errMsg } from "../../components/Toast";
import CapacityBar from "../../components/CapacityBar";
import { SectionCard } from "../ui";
import { useXsFullScreen } from "../../responsive";
import { NumberField } from "../../components/NumberField";

export default function PortalPanels() {
  const xsFull = useXsFullScreen();
  const { data = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-panels"],
    queryFn: portalPanels,
  });
  const { data: caps = [], isLoading: capsLoading } = useQuery({
    queryKey: ["portal-capacity"],
    queryFn: portalCapacity,
  });
  const { node: toast, show } = useToast();
  const [reqRow, setReqRow] = useState<PortalCapacity | null>(null);
  const [reqAmount, setReqAmount] = useState("");
  const [reqNote, setReqNote] = useState("");
  const [busy, setBusy] = useState(false);

  const copy = async (link: string) => {
    try { await navigator.clipboard.writeText(link); show("لینک کپی شد", "success"); }
    catch { show("کپی نشد؛ لینک را دستی انتخاب کنید", "error"); }
  };

  const openReq = (row: PortalCapacity) => { setReqRow(row); setReqAmount(""); setReqNote(""); };

  const sendReq = async () => {
    if (!reqRow) return;
    setBusy(true);
    try {
      const amount = reqAmount ? parseInt(reqAmount, 10) : undefined;
      await portalRequestCapacity({ reseller_id: reqRow.reseller_id, amount, note: reqNote || undefined });
      show("درخواستِ افزایشِ ظرفیت برای مالک ارسال شد", "success");
      setReqRow(null);
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  return (
    <Box>
      {toast}
      <Typography variant="h5" sx={{ mb: 0.4 }}>پنل‌ها و ظرفیت</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        لینکِ مدیریتِ شما روی هر پنل + میزانِ پُریِ ظرفیتِ کاربرانتان
      </Typography>

      {/* ظرفیتِ من */}
      <Box sx={{ mb: 3 }}>
        <SectionCard title="ظرفیتِ من">
          <DataState isLoading={capsLoading} rows={2}>
            {caps.length === 0 ? (
              <Typography variant="body2" color="text.secondary">داده‌ای برای نمایش وجود ندارد.</Typography>
            ) : (
              <Stack spacing={1.5}>
                {caps.map((c) => (
                  <Stack key={c.reseller_id} direction="row" alignItems="center" justifyContent="space-between" spacing={2}
                    sx={{ p: 1.2, borderRadius: 2, bgcolor: "action.hover" }}>
                    <Box sx={{ minWidth: 0 }}>
                      <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>{c.name}</Typography>
                      <Typography variant="caption" color="text.secondary">{c.panel_key}</Typography>
                    </Box>
                    <Box sx={{ flex: 1, maxWidth: 220 }}><CapacityBar used={c.used} max={c.max} /></Box>
                    <Button size="small" variant="outlined" startIcon={<TrendingUpIcon sx={{ fontSize: 17 }} />}
                      onClick={() => openReq(c)}>درخواستِ افزایش</Button>
                  </Stack>
                ))}
              </Stack>
            )}
          </DataState>
        </SectionCard>
      </Box>

      <DataState isLoading={isLoading} isError={isError} onRetry={refetch} rows={3}>
        {data.length === 0 ? (
          <Card><Box sx={{ p: 3, textAlign: "center", color: "text.secondary" }}>پنلی برای شما ثبت نشده است.</Box></Card>
        ) : (
          <Grid container spacing={2}>
            {data.map((p) => (
              <Grid item xs={12} md={6} key={p.reseller_id}>
                <Card sx={{ height: "100%" }}>
                  <CardContent>
                    <Stack direction="row" alignItems="center" spacing={1.2} sx={{ mb: 1.5 }}>
                      <Box sx={{ width: 31, height: 31, borderRadius: 2, display: "grid", placeItems: "center", bgcolor: "action.hover", color: "primary.main" }}>
                        <DnsIcon fontSize="small" />
                      </Box>
                      <Box sx={{ minWidth: 0 }}>
                        <Typography variant="subtitle1" noWrap sx={{ fontWeight: 800 }}>{p.panel_name}</Typography>
                        <Typography variant="caption" color="text.secondary">{p.name}</Typography>
                      </Box>
                    </Stack>

                    <Box
                      dir="ltr"
                      tabIndex={0}
                      role="group"
                      sx={{
                        p: 1.2, borderRadius: 2, bgcolor: "action.hover",
                        fontFamily: "monospace", fontSize: 12.5, wordBreak: "break-all",
                        display: "flex", alignItems: "center", gap: 1,
                      }}
                    >
                      <Box sx={{ flex: 1, minWidth: 0 }}>{p.link}</Box>
                      <Tooltip title="کپی لینک">
                        <IconButton size="small" onClick={() => copy(p.link)} aria-label="کپی لینک پنل">
                          <ContentCopyIcon sx={{ fontSize: 17 }} />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title="باز کردن">
                        <IconButton size="small" component="a" href={p.link} target="_blank" rel="noopener noreferrer" aria-label="باز کردن لینک پنل">
                          <OpenInNewIcon sx={{ fontSize: 17 }} />
                        </IconButton>
                      </Tooltip>
                    </Box>
                  </CardContent>
                </Card>
              </Grid>
            ))}
          </Grid>
        )}
      </DataState>

      <Dialog open={!!reqRow} onClose={busy ? undefined : () => setReqRow(null)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <DialogTitle sx={{ fontWeight: 800 }}>درخواستِ افزایشِ ظرفیت — {reqRow?.name}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            این درخواست به‌همراهِ ظرفیتِ فعلیِ شما برای مالک ارسال می‌شود. مقدارِ پیشنهادی و یادداشت
            اختیاری است.
          </Typography>
          <NumberField fullWidth label="مقدارِ پیشنهادی (اختیاری)" value={reqAmount}
            onChange={setReqAmount} sx={{ mb: 1.5 }} />
          <TextField fullWidth multiline minRows={2} label="یادداشت (اختیاری)" value={reqNote}
            onChange={(e) => setReqNote(e.target.value)} inputProps={{ maxLength: 500 }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setReqRow(null)} disabled={busy}>انصراف</Button>
          <Button variant="contained" onClick={sendReq} disabled={busy}>ارسالِ درخواست</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
