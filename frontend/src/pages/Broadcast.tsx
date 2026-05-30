import { useState } from "react";
import {
  Box, Card, CardContent, Typography, TextField, Button, Stack, Divider, Alert, MenuItem,
} from "@mui/material";
import CampaignIcon from "@mui/icons-material/Campaign";
import CleaningServicesIcon from "@mui/icons-material/CleaningServices";
import { useMutation, useQuery } from "@tanstack/react-query";
import { broadcastMessage, runChannelGuard, listPanels } from "../api/client";
import { useToast, errMsg } from "../components/Toast";

export default function Broadcast() {
  const { node, show } = useToast();
  const [text, setText] = useState("");
  const [audience, setAudience] = useState("all");
  const [panelId, setPanelId] = useState<string>("");
  const [result, setResult] = useState<string>("");
  const { data: panels = [] } = useQuery({ queryKey: ["panels"], queryFn: listPanels });

  const send = useMutation({
    mutationFn: () => broadcastMessage({
      text, audience,
      panel_id: audience === "panel" && panelId ? Number(panelId) : undefined,
    }),
    onSuccess: (r: any) => {
      setResult(`ارسال شد: ${r.sent} موفق، ${r.blocked} مسدود، ${r.failed} ناموفق (از ${r.total} گیرنده)`);
      show("پیام همگانی ارسال شد");
      setText("");
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
        r.dry_run ? "info" : "success"
      );
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  return (
    <Box sx={{ maxWidth: 720 }}>
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <CampaignIcon color="primary" />
            <Typography variant="h6">پیام همگانی</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            پیام به گروهِ انتخاب‌شده از نماینده‌های ثبت‌شده در ربات ارسال می‌شود.
          </Typography>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }}>
            <TextField select label="گیرندگان" value={audience} sx={{ minWidth: 220 }}
              onChange={(e) => setAudience(e.target.value)}>
              <MenuItem value="all">همه نمایندگان</MenuItem>
              <MenuItem value="debtors">فقط بدهکاران</MenuItem>
              <MenuItem value="zero_sale">فروش صفر این ماه</MenuItem>
              <MenuItem value="panel">نمایندگان یک پنل</MenuItem>
            </TextField>
            {audience === "panel" && (
              <TextField select label="پنل" value={panelId} sx={{ minWidth: 160 }}
                onChange={(e) => setPanelId(e.target.value)}>
                {panels.map((p: any) => <MenuItem key={p.id} value={p.id}>{p.key}</MenuItem>)}
              </TextField>
            )}
          </Stack>
          <TextField
            label="متن پیام" value={text} onChange={(e) => setText(e.target.value)}
            multiline minRows={5} fullWidth sx={{ mb: 2 }}
          />
          {result && <Alert severity="success" sx={{ mb: 2 }}>{result}</Alert>}
          <Button variant="contained" startIcon={<CampaignIcon />}
            disabled={!text.trim() || send.isPending || (audience === "panel" && !panelId)}
            onClick={() => send.mutate()}>
            ارسال
          </Button>
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
