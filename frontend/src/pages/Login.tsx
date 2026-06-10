import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Box, Button, Card, CardContent, TextField, Typography, Alert, Stack, IconButton, Tooltip, Divider,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import FingerprintIcon from "@mui/icons-material/esm/Fingerprint";
import { startAuthentication } from "@simplewebauthn/browser";
import { useAuth } from "../auth/AuthContext";
import { getCaptcha, login, passkeyLoginBegin, passkeyLoginComplete } from "../api/client";

const passkeySupported =
  typeof window !== "undefined" && !!window.PublicKeyCredential;

export default function Login() {
  const { finishLogin, authed } = useAuth();
  const nav = useNavigate();
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [captchaId, setCaptchaId] = useState("");
  const [captchaImg, setCaptchaImg] = useState("");
  const [captchaAns, setCaptchaAns] = useState("");
  const [need2fa, setNeed2fa] = useState(false);
  const [totp, setTotp] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const loadCaptcha = async () => {
    try {
      const c = await getCaptcha();
      setCaptchaId(c.captcha_id);
      setCaptchaImg(c.image);
      setCaptchaAns("");
    } catch { /* backend offline */ }
  };

  useEffect(() => { loadCaptcha(); }, []);
  useEffect(() => { if (authed) nav("/", { replace: true }); }, [authed]);

  const loginWithPasskey = async () => {
    setErr(""); setBusy(true);
    try {
      const { handle, options } = await passkeyLoginBegin();
      const credential = await startAuthentication({ optionsJSON: options });
      const { access_token } = await passkeyLoginComplete({ handle, credential });
      await finishLogin(access_token);
      nav("/", { replace: true });
    } catch (e: any) {
      // a user cancelling the Face ID sheet isn't an error worth shouting about
      if (e?.name !== "NotAllowedError" && e?.name !== "AbortError") {
        setErr(e?.response?.data?.detail || "ورود با Face ID ناموفق بود. با رمز وارد شوید.");
      }
    } finally { setBusy(false); }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const { access_token } = await login({
        username: u, password: p, captcha_id: captchaId, captcha_answer: captchaAns,
        totp_code: need2fa ? totp : undefined,
      });
      await finishLogin(access_token);
      nav("/", { replace: true });
    } catch (e: any) {
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail;
      if (status === 401 && e?.response?.headers?.["x-2fa-required"]) {
        setNeed2fa(true);
        // The password step already consumed the captcha (single-use), so the 2FA submit needs
        // a fresh one — load it and ask the user to re-enter it alongside the 2FA code.
        await loadCaptcha();
        setErr("کد تأیید دو مرحله‌ای و کد امنیتیِ جدید را وارد کنید.");
      } else {
        setErr(detail || "ورود ناموفق بود.");
        await loadCaptcha(); // captcha is single-use → refresh on any failure
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ display: "grid", placeItems: "center", minHeight: "100vh", p: 2 }}>
      <Card sx={{ width: 400, maxWidth: "100%" }}>
        <CardContent sx={{ p: 4 }}>
          <Typography variant="h5" color="primary" align="center" gutterBottom>
            سامانه مدیریت فاکتور
          </Typography>
          <Typography variant="body2" color="text.secondary" align="center" sx={{ mb: 3 }}>
            ورود مدیر سیستم
          </Typography>
          <form onSubmit={submit}>
            {err && <Alert severity={need2fa ? "info" : "error"} sx={{ mb: 2 }}>{err}</Alert>}
            {passkeySupported && (
              <>
                <Button fullWidth size="large" variant="outlined" startIcon={<FingerprintIcon />}
                  onClick={loginWithPasskey} disabled={busy} sx={{ mb: 2 }}>
                  ورود با Face ID / کلید عبور
                </Button>
                <Divider sx={{ mb: 2, fontSize: 12.5, color: "text.secondary" }}>یا با رمز عبور</Divider>
              </>
            )}
            {/* autoComplete + name so iCloud Keychain / password managers fill BOTH fields */}
            <TextField label="نام کاربری" fullWidth value={u} name="username" autoComplete="username"
              onChange={(e) => setU(e.target.value)} sx={{ mb: 2 }} autoFocus />
            <TextField label="رمز عبور" type="password" fullWidth value={p} name="password"
              autoComplete="current-password"
              onChange={(e) => setP(e.target.value)} sx={{ mb: 2 }} />

            {/* captcha */}
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
              <Box sx={{ borderRadius: 1, overflow: "hidden", border: "1px solid", borderColor: "divider", height: 56 }}>
                {captchaImg ? <img src={captchaImg} alt="تصویر کد امنیتی" height={56} /> : <Box sx={{ width: 160, height: 56 }} />}
              </Box>
              <Tooltip title="کد جدید"><IconButton onClick={loadCaptcha}><RefreshIcon /></IconButton></Tooltip>
            </Stack>
            <TextField label="کد امنیتی تصویر" fullWidth value={captchaAns} autoComplete="off"
              onChange={(e) => setCaptchaAns(e.target.value)} sx={{ mb: 2 }} inputProps={{ maxLength: 6, dir: "ltr" }} />

            {need2fa && (
              <TextField label="کد تأیید دو مرحله‌ای (Authenticator)" fullWidth value={totp}
                autoComplete="one-time-code"
                onChange={(e) => setTotp(e.target.value)} sx={{ mb: 2 }} inputProps={{ maxLength: 6, dir: "ltr" }} autoFocus />
            )}

            <Button type="submit" variant="contained" fullWidth size="large" disabled={busy}>
              {busy ? "در حال ورود..." : "ورود"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </Box>
  );
}
