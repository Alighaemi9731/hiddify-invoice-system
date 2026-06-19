import { useState } from "react";
import { Box, Button, Card, CardContent, Stack, TextField, Typography } from "@mui/material";
import SendIcon from "@mui/icons-material/esm/Send";
import SupportAgentIcon from "@mui/icons-material/esm/SupportAgent";
import { portalSupport } from "../portalClient";
import { useToast, errMsg } from "../../components/Toast";

export default function PortalSupport() {
  const { node: toast, show } = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    const t = text.trim();
    if (!t) { show("پیام را بنویسید", "warning"); return; }
    setBusy(true);
    try {
      await portalSupport(t);
      show("پیام شما برای پشتیبانی ارسال شد؛ پاسخ از طریق ربات تلگرام به شما می‌رسد.", "success");
      setText("");
    } catch (e) { show(errMsg(e), "error"); }
    finally { setBusy(false); }
  };

  return (
    <Box>
      {toast}
      <Typography variant="h5" sx={{ mb: 0.4 }}>پشتیبانی</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
        پیامتان مستقیم به پشتیبانی می‌رسد؛ پاسخ از طریقِ ربات تلگرامِ شما ارسال می‌شود
      </Typography>

      <Card sx={{ maxWidth: 640 }}>
        <CardContent>
          <Stack spacing={2}>
            <Stack direction="row" spacing={1.2} alignItems="center" sx={{ color: "primary.main" }}>
              <SupportAgentIcon />
              <Typography sx={{ fontWeight: 700 }}>پیام به پشتیبانی</Typography>
            </Stack>
            <TextField
              multiline
              minRows={5}
              fullWidth
              placeholder="پیام خود را بنویسید…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              inputProps={{ maxLength: 4000 }}
            />
            <Box sx={{ display: "flex", justifyContent: "flex-end" }}>
              <Button
                variant="contained"
                startIcon={<SendIcon />}
                onClick={send}
                disabled={busy || !text.trim()}
              >
                ارسال
              </Button>
            </Box>
          </Stack>
        </CardContent>
      </Card>
    </Box>
  );
}
