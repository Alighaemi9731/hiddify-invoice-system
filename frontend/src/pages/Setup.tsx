import { useState, useEffect } from "react";
import {
  Box, Button, Card, CardContent, TextField, Typography, Alert, Stack, CircularProgress, Link,
} from "@mui/material";
import RocketLaunchIcon from "@mui/icons-material/esm/RocketLaunch";
import { doSetup, getSetupStatus, prepareSetupDomain } from "../api/client";

export default function Setup({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [token, setToken] = useState("");
  const [tokenRequired, setTokenRequired] = useState(false);
  const [domain, setDomain] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<any>(null);
  const needsHttps = location.protocol !== "https:" &&
    !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);

  // F1: the installer may require a one-time bootstrap token to complete first-run setup.
  useEffect(() => {
    getSetupStatus().then((s) => {
      setTokenRequired(!!s.token_required);
      setDomain(s.domain || "");
    }).catch(() => {});
  }, []);

  const valid =
    needsHttps ? domain.trim().length > 0 && token.trim().length > 0 :
      username.trim().length >= 3 && password.length >= 8 && password === password2 &&
      (!tokenRequired || token.trim().length > 0);

  const submit = async () => {
    setErr("");
    if (!valid) {
      setErr("نام کاربری حداقل ۳ و رمز حداقل ۸ کاراکتر، و دو رمز یکسان باشند.");
      return;
    }
    setBusy(true);
    try {
      if (needsHttps) {
        const r = await prepareSetupDomain({
          domain: domain.trim(), token: token.trim(), acme_email: email.trim() || undefined,
        });
        setResult({ ...r, tls_prepared: true, domain_applied: r.ok });
        return;
      }
      const r = await doSetup({
        username: username.trim(), password,
        token: token.trim() || undefined,
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
            <Typography variant="h5" gutterBottom>{result.tls_prepared ? "اتصال امن آماده می‌شود" : "راه‌اندازی کامل شد"}</Typography>
            {url && ok ? (
              <>
                <Alert severity="success" sx={{ my: 2, textAlign: "right" }}>
                  {result.tls_prepared
                    ? "دامنه ثبت شد و گواهی SSL در حال صدور است. برای ساخت حساب مدیر، آدرس زیر را باز کنید و توکن نصب را دوباره وارد کنید:"
                    : "دامنه ثبت شد و گواهی SSL در حال صدور است. از طریق آدرس زیر وارد شوید:"}
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
          {needsHttps && <Alert severity="info" sx={{ mb: 2 }}>
            ابتدا دامنه و HTTPS را فعال کنید؛ حساب مدیر و رمز عبور در مرحلهٔ بعد، روی اتصال امن ساخته می‌شوند.
            رکورد A دامنه باید به IP این سرور اشاره کند. توکن نصب را از کنسول سرور بردارید.
          </Alert>}

          <Stack spacing={2}>
            {!needsHttps && <>
            <Typography variant="subtitle2" color="text.secondary">۱) حساب مدیر</Typography>
            <TextField label="نام کاربری" inputProps={{ dir: "ltr" }} value={username} onChange={(e) => setUsername(e.target.value)} fullWidth />
            <TextField label="رمز عبور" type="password" value={password} onChange={(e) => setPassword(e.target.value)} fullWidth
              helperText="حداقل ۸ کاراکتر" />
            <TextField label="تکرار رمز عبور" type="password" value={password2} onChange={(e) => setPassword2(e.target.value)} fullWidth
              error={!!password2 && password !== password2} />
            </>}

            {(tokenRequired || needsHttps) && (
              <TextField label="توکن راه‌اندازی" inputProps={{ dir: "ltr" }} value={token} onChange={(e) => setToken(e.target.value)} fullWidth
                helperText="این توکن هنگام نصب در کنسول سرور نمایش داده شده است (SETUP_BOOTSTRAP_TOKEN)." />
            )}

            <Typography variant="subtitle2" color="text.secondary" sx={{ mt: 1 }}>{needsHttps ? "دامنه و اتصال امن" : "۲) دامنه (اختیاری)"}</Typography>
            <TextField label="دامنه" inputProps={{ dir: "ltr" }} placeholder="panel.example.com" value={domain} onChange={(e) => setDomain(e.target.value)} fullWidth
              helperText={needsHttps ? "برای ساخت حساب مدیر از طریق اینترنت، دامنه و HTTPS لازم است." : "رکورد A دامنه باید به IP این سرور اشاره کند."} />
            <TextField label="ایمیل برای گواهی SSL" inputProps={{ dir: "ltr" }} value={email} onChange={(e) => setEmail(e.target.value)} fullWidth />

            <Button variant="contained" size="large" disabled={!valid || busy} onClick={submit}
              startIcon={busy ? <CircularProgress size={18} color="inherit" /> : <RocketLaunchIcon />}>
              {busy ? "در حال راه‌اندازی…" : needsHttps ? "فعال‌سازی HTTPS و ادامه" : "تکمیل راه‌اندازی"}
            </Button>
          </Stack>
        </CardContent>
      </Card>
    </Box>
  );
}
