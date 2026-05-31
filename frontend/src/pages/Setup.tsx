import { useState } from "react";
import {
  Box, Button, Card, CardContent, TextField, Typography, Alert, Stack, CircularProgress, Link,
} from "@mui/material";
import RocketLaunchIcon from "@mui/icons-material/RocketLaunch";
import { doSetup } from "../api/client";

export default function Setup({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [domain, setDomain] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<any>(null);

  const valid =
    username.trim().length >= 3 && password.length >= 8 && password === password2;

  const submit = async () => {
    setErr("");
    if (!valid) {
      setErr("نام کاربری حداقل ۳ و رمز حداقل ۸ کاراکتر، و دو رمز یکسان باشند.");
      return;
    }
    setBusy(true);
    try {
      const r = await doSetup({
        username: username.trim(), password,
        domain: domain.trim() || undefined, acme_email: email.trim() || undefined,
      });
      setResult(r);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || "راه‌اندازی ناموفق بود.");
    } finally {
      setBusy(false);
    }
  };

  // ---- success screen ----
  if (result) {
    const url = result.url as string | undefined;
    const ok = result.domain_applied;
    return (
      <Box sx={{ display: "grid", placeItems: "center", minHeight: "100vh", p: 2 }}>
        <Card sx={{ width: 460, maxWidth: "100%" }}>
          <CardContent sx={{ p: 4, textAlign: "center" }}>
            <RocketLaunchIcon color="success" sx={{ fontSize: 48, mb: 1 }} />
            <Typography variant="h5" gutterBottom>راه‌اندازی کامل شد</Typography>
            {url && ok ? (
              <>
                <Alert severity="success" sx={{ my: 2, textAlign: "right" }}>
                  دامنه ثبت شد و گواهی SSL در حال صدور است. از طریق آدرس زیر وارد شوید:
                </Alert>
                <Link href={url} variant="h6" dir="ltr" sx={{ display: "block", mb: 2 }}>{url}</Link>
                <Typography variant="caption" color="text.secondary">
                  اگر بلافاصله باز نشد، چند ثانیه صبر کنید تا گواهی صادر شود.
                </Typography>
              </>
            ) : url && !ok ? (
              <Alert severity="warning" sx={{ my: 2, textAlign: "right" }}>
                {result.message || "ثبت دامنه ناموفق بود؛ روی همین آدرس IP باقی می‌مانید."}
                {" "}بعداً از «حساب و پشتیبان → دامنه و HTTPS» دوباره تلاش کنید.
              </Alert>
            ) : (
              <Alert severity="success" sx={{ my: 2, textAlign: "right" }}>
                حساب مدیر ساخته شد. اکنون وارد شوید.
              </Alert>
            )}
            <Button variant="contained" fullWidth size="large" sx={{ mt: 1 }}
              onClick={() => { if (url && ok) location.href = url; else onDone(); }}>
              {url && ok ? "ورود از طریق دامنه" : "رفتن به صفحهٔ ورود"}
            </Button>
          </CardContent>
        </Card>
      </Box>
    );
  }

  // ---- wizard ----
  return (
    <Box sx={{ display: "grid", placeItems: "center", minHeight: "100vh", p: 2 }}>
      <Card sx={{ width: 480, maxWidth: "100%" }}>
        <CardContent sx={{ p: 4 }}>
          <Stack direction="row" spacing={1.5} alignItems="center" justifyContent="center" sx={{ mb: 1 }}>
            <RocketLaunchIcon color="primary" />
            <Typography variant="h5" color="primary">راه‌اندازی اولیه</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" align="center" sx={{ mb: 3 }}>
            حساب مدیر و دامنهٔ پنل را تنظیم کنید. این صفحه فقط یک‌بار نمایش داده می‌شود.
          </Typography>

          {err && <Alert severity="error" sx={{ mb: 2 }}>{err}</Alert>}

          <Stack spacing={2}>
            <Typography variant="subtitle2" color="text.secondary">۱) حساب مدیر</Typography>
            <TextField label="نام کاربری" dir="ltr" value={username} onChange={(e) => setUsername(e.target.value)} fullWidth />
            <TextField label="رمز عبور" type="password" value={password} onChange={(e) => setPassword(e.target.value)} fullWidth
              helperText="حداقل ۸ کاراکتر" />
            <TextField label="تکرار رمز عبور" type="password" value={password2} onChange={(e) => setPassword2(e.target.value)} fullWidth
              error={!!password2 && password !== password2} />

            <Typography variant="subtitle2" color="text.secondary" sx={{ mt: 1 }}>۲) دامنه (اختیاری)</Typography>
            <TextField label="دامنه" dir="ltr" placeholder="panel.example.com" value={domain} onChange={(e) => setDomain(e.target.value)} fullWidth
              helperText="رکورد A دامنه باید به IP این سرور اشاره کند. خالی بگذارید تا فعلاً روی IP بماند." />
            <TextField label="ایمیل برای گواهی SSL" dir="ltr" value={email} onChange={(e) => setEmail(e.target.value)} fullWidth />

            <Button variant="contained" size="large" disabled={!valid || busy} onClick={submit}
              startIcon={busy ? <CircularProgress size={18} color="inherit" /> : <RocketLaunchIcon />}>
              {busy ? "در حال راه‌اندازی…" : "تکمیل راه‌اندازی"}
            </Button>
          </Stack>
        </CardContent>
      </Card>
    </Box>
  );
}
