import { useRef, useState } from "react";
import {
  Box, Card, CardContent, Typography, TextField, Button, Stack, Alert, Divider, Chip,
} from "@mui/material";
import LockResetIcon from "@mui/icons-material/LockReset";
import CloudDownloadIcon from "@mui/icons-material/CloudDownload";
import TelegramIcon from "@mui/icons-material/Telegram";
import RestoreIcon from "@mui/icons-material/Restore";
import DeleteForeverIcon from "@mui/icons-material/DeleteForever";
import ShieldIcon from "@mui/icons-material/Shield";
import LanguageIcon from "@mui/icons-material/Language";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  updateAccount, downloadBackup, sendBackupToTelegram, restoreBackup, setToken, wipeData,
  getMe, totpSetup, totpEnable, totpDisable, setDomain, restartService,
} from "../api/client";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import { useToast, errMsg } from "../components/Toast";

export default function AccountBackup() {
  const { node, show } = useToast();
  const qc = useQueryClient();
  const [cur, setCur] = useState("");
  const [newUser, setNewUser] = useState("");
  const [newPass, setNewPass] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  // 2FA state
  const { data: me } = useQuery({ queryKey: ["me"], queryFn: getMe });
  const [setup, setSetup] = useState<any>(null);   // {secret, otpauth_uri, qr}
  const [code, setCode] = useState("");
  const [disablePass, setDisablePass] = useState("");

  // domain / SSL
  const [domain, setDomainVal] = useState("");
  const [acmeEmail, setAcmeEmail] = useState("");
  const [domainResult, setDomainResult] = useState("");
  const domainMut = useMutation({
    mutationFn: () => setDomain(domain, acmeEmail || undefined),
    onSuccess: (r: any) => { setDomainResult(r.message || ""); show(r.ok ? "دامنه اعمال شد" : "دامنه ذخیره شد", r.ok ? "success" : "info"); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const startSetup = useMutation({
    mutationFn: () => totpSetup(),
    onSuccess: (r: any) => setSetup(r),
    onError: (e) => show(errMsg(e), "error"),
  });
  const enable2fa = useMutation({
    mutationFn: () => totpEnable(code),
    onSuccess: () => { show("ورود دو مرحله‌ای فعال شد", "success"); setSetup(null); setCode(""); qc.invalidateQueries({ queryKey: ["me"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });
  const disable2fa = useMutation({
    mutationFn: () => totpDisable(disablePass),
    onSuccess: () => { show("ورود دو مرحله‌ای غیرفعال شد"); setDisablePass(""); qc.invalidateQueries({ queryKey: ["me"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });

  const save = useMutation({
    mutationFn: () => updateAccount({
      current_password: cur,
      new_username: newUser || undefined,
      new_password: newPass || undefined,
    }),
    onSuccess: (r: any) => {
      if (r?.access_token) setToken(r.access_token);
      show("حساب به‌روزرسانی شد");
      setCur(""); setNewUser(""); setNewPass("");
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  const sendTg = useMutation({
    mutationFn: () => sendBackupToTelegram(),
    onSuccess: (r: any) => show(
      r?.status === "ok" ? "پشتیبان به تلگرام شما ارسال شد" : `وضعیت: ${r?.status}`,
      r?.status === "ok" ? "success" : "info",
    ),
    onError: (e) => show(errMsg(e), "error"),
  });

  const restore = useMutation({
    mutationFn: (f: File) => restoreBackup(f),
    onSuccess: (r: any) => show(`بازیابی: ${r.note || r.status}`, "success"),
    onError: (e) => show(errMsg(e), "error"),
  });

  const wipe = useMutation({
    mutationFn: () => wipeData(),
    onSuccess: (r: any) => show(r.message || "داده‌ها پاک شد", "success"),
    onError: (e) => show(errMsg(e), "error"),
  });

  const restart = useMutation({
    mutationFn: () => restartService(),
    onSuccess: () => show("سرویس در حال راه‌اندازی مجدد است؛ چند ثانیه صبر کنید و صفحه را تازه کنید.", "info"),
    onError: (e) => show(errMsg(e), "error"),
  });

  return (
    <Stack spacing={2} sx={{ maxWidth: 720 }}>
      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <LockResetIcon color="primary" />
            <Typography variant="h6">تغییر نام کاربری و رمز عبور</Typography>
          </Stack>
          <Stack spacing={2}>
            <TextField label="رمز عبور فعلی *" type="password" value={cur}
              onChange={(e) => setCur(e.target.value)} />
            <TextField label="نام کاربری جدید (اختیاری)" value={newUser} dir="ltr"
              onChange={(e) => setNewUser(e.target.value)} />
            <TextField label="رمز عبور جدید (اختیاری)" type="password" value={newPass}
              onChange={(e) => setNewPass(e.target.value)} />
            <Box>
              <Button variant="contained" startIcon={<LockResetIcon />}
                disabled={!cur || (!newUser && !newPass) || save.isPending}
                onClick={() => save.mutate()}>
                ذخیره تغییرات حساب
              </Button>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      {/* 2FA */}
      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <ShieldIcon color={me?.totp_enabled ? "success" : "primary"} />
            <Typography variant="h6">ورود دو مرحله‌ای (Google Authenticator)</Typography>
            {me?.totp_enabled && <Chip size="small" color="success" label="فعال" />}
          </Stack>

          {me?.totp_enabled ? (
            <Stack spacing={2}>
              <Alert severity="success">ورود دو مرحله‌ای فعال است. برای غیرفعال‌سازی رمز عبور را وارد کنید.</Alert>
              <TextField label="رمز عبور فعلی" type="password" value={disablePass}
                onChange={(e) => setDisablePass(e.target.value)} sx={{ maxWidth: 320 }} />
              <Box>
                <Button color="error" variant="outlined" disabled={!disablePass || disable2fa.isPending}
                  onClick={() => disable2fa.mutate()}>غیرفعال‌سازی</Button>
              </Box>
            </Stack>
          ) : setup ? (
            <Stack spacing={2}>
              <Typography variant="body2" color="text.secondary">
                ۱) برنامهٔ Google Authenticator (یا مشابه) را باز کنید و این QR را اسکن کنید.
                ۲) سپس کد ۶ رقمی را وارد و تأیید کنید.
              </Typography>
              <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap", alignItems: "center" }}>
                <img src={setup.qr} alt="2FA QR" width={170} height={170}
                  style={{ border: "1px solid #e5e7eb", borderRadius: 8 }} />
                <Box>
                  <Typography variant="caption" color="text.secondary">کلید دستی:</Typography>
                  <Typography dir="ltr" sx={{ fontFamily: "monospace", wordBreak: "break-all", mb: 2 }}>{setup.secret}</Typography>
                  <TextField label="کد ۶ رقمی" value={code} dir="ltr" inputProps={{ maxLength: 6 }}
                    onChange={(e) => setCode(e.target.value)} sx={{ mb: 1, display: "block" }} />
                  <Button variant="contained" disabled={code.length < 6 || enable2fa.isPending}
                    onClick={() => enable2fa.mutate()}>تأیید و فعال‌سازی</Button>
                </Box>
              </Box>
            </Stack>
          ) : (
            <Stack spacing={2}>
              <Typography variant="body2" color="text.secondary">
                با فعال‌سازی، هنگام ورود علاوه بر رمز، یک کد یک‌بارمصرف از اپلیکیشن Authenticator هم لازم می‌شود.
              </Typography>
              <Box>
                <Button variant="contained" startIcon={<ShieldIcon />}
                  disabled={startSetup.isPending} onClick={() => startSetup.mutate()}>
                  راه‌اندازی ورود دو مرحله‌ای
                </Button>
              </Box>
            </Stack>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <CloudDownloadIcon color="primary" />
            <Typography variant="h6">پشتیبان‌گیری و بازیابی</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            هر ۲ ساعت یک پشتیبان کامل (دیتابیس + تنظیمات) به‌صورت خودکار به پی‌وی تلگرام شما
            ارسال می‌شود. می‌توانید همین حالا هم پشتیبان بگیرید یا یک فایل پشتیبان را برای
            بازیابی بارگذاری کنید.
          </Typography>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }}>
            <Button variant="outlined" startIcon={<CloudDownloadIcon />}
              onClick={() => downloadBackup()}>دانلود پشتیبان</Button>
            <Button variant="outlined" startIcon={<TelegramIcon />}
              disabled={sendTg.isPending} onClick={() => sendTg.mutate()}>ارسال به تلگرام</Button>
          </Stack>
          <Divider sx={{ my: 2 }} />
          <Alert severity="warning" sx={{ mb: 2 }}>
            بازیابی، دیتابیس فعلی را با فایل پشتیبان جایگزین می‌کند. پس از بازیابی، سرویس بک‌اند
            باید یک‌بار ری‌استارت شود.
          </Alert>
          <input ref={fileRef} type="file" accept=".zip" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) restore.mutate(f); e.currentTarget.value = ""; }} />
          <Button variant="contained" color="warning" startIcon={<RestoreIcon />}
            disabled={restore.isPending} onClick={() => fileRef.current?.click()}>
            بارگذاری فایل پشتیبان و بازیابی
          </Button>
          <Divider sx={{ my: 2 }} />
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            اگر لازم شد سرویس را یک‌بار راه‌اندازی مجدد کنید، از این دکمه استفاده کنید — نیازی به
            ترمینال سرور نیست. (بازیابی به‌صورت خودکار این کار را انجام می‌دهد.)
          </Typography>
          <Button variant="outlined" startIcon={<RestartAltIcon />} disabled={restart.isPending}
            onClick={() => { if (confirm("سرویس یک‌بار راه‌اندازی مجدد شود؟")) restart.mutate(); }}>
            راه‌اندازی مجدد سرویس
          </Button>
        </CardContent>
      </Card>

      {/* Domain & HTTPS */}
      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <LanguageIcon color="primary" />
            <Typography variant="h6">دامنه و HTTPS</Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            دامنهٔ خود را وارد کنید (رکورد A آن باید به IP این سرور اشاره کند). سامانه به‌صورت
            خودکار گواهی SSL می‌گیرد و پنل روی همان دامنه با HTTPS در دسترس می‌شود.
          </Typography>
          <Stack spacing={2} sx={{ maxWidth: 420 }}>
            <TextField label="دامنه" dir="ltr" placeholder="panel.example.com"
              value={domain} onChange={(e) => setDomainVal(e.target.value)} />
            <TextField label="ایمیل برای گواهی SSL (اختیاری)" dir="ltr"
              value={acmeEmail} onChange={(e) => setAcmeEmail(e.target.value)} />
            {domainResult && <Alert severity="success">{domainResult}</Alert>}
            <Box>
              <Button variant="contained" startIcon={<LanguageIcon />}
                disabled={!domain.trim() || domainMut.isPending}
                onClick={() => domainMut.mutate()}>
                ثبت دامنه و فعال‌سازی HTTPS
              </Button>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      <Card sx={{ borderColor: "error.main" }}>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <DeleteForeverIcon color="error" />
            <Typography variant="h6">پاک‌سازی کامل داده‌ها</Typography>
          </Stack>
          <Alert severity="error" sx={{ mb: 2 }}>
            همهٔ پنل‌ها، نمایندگان، فاکتورها، پرداخت‌ها و گزارش‌ها حذف می‌شوند. حساب مدیر و
            تنظیمات (توکن، کیف پول، …) حفظ می‌شوند. این عمل غیرقابل بازگشت است — قبل از آن
            حتماً یک پشتیبان بگیرید.
          </Alert>
          <Button variant="contained" color="error" startIcon={<DeleteForeverIcon />}
            disabled={wipe.isPending}
            onClick={() => {
              if (prompt("برای حذف همهٔ داده‌ها عبارت DELETE را تایپ کنید:") === "DELETE") wipe.mutate();
            }}>
            پاک‌سازی همهٔ داده‌ها
          </Button>
        </CardContent>
      </Card>
      {node}
    </Stack>
  );
}
