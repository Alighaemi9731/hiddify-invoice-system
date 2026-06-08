import { useEffect, useRef, useState } from "react";
import {
  Box, Card, CardContent, Typography, TextField, Button, Stack, Alert, Divider, Chip,
  LinearProgress,
} from "@mui/material";
import LockResetIcon from "@mui/icons-material/LockReset";
import CloudDownloadIcon from "@mui/icons-material/CloudDownload";
import TelegramIcon from "@mui/icons-material/Telegram";
import RestoreIcon from "@mui/icons-material/Restore";
import DeleteForeverIcon from "@mui/icons-material/DeleteForever";
import ShieldIcon from "@mui/icons-material/Shield";
import FingerprintIcon from "@mui/icons-material/Fingerprint";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import LanguageIcon from "@mui/icons-material/Language";
import SystemUpdateAltIcon from "@mui/icons-material/SystemUpdateAlt";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  updateAccount, downloadBackup, sendBackupToTelegram, restoreBackup, setToken, wipeData,
  getMe, totpSetup, totpEnable, totpDisable, setDomain, restartService,
  getInfo, updateSystem, getUpdateStatus, pingInfo,
  passkeyList, passkeyRegisterBegin, passkeyRegisterComplete, passkeyDelete,
} from "../api/client";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import { startRegistration } from "@simplewebauthn/browser";
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

  // Passkey (Face ID / WebAuthn)
  const passkeySupported = typeof window !== "undefined" && !!window.PublicKeyCredential;
  const { data: passkeys = [] } = useQuery({ queryKey: ["passkeys"], queryFn: passkeyList, enabled: passkeySupported });
  const addPasskey = useMutation({
    mutationFn: async () => {
      const { handle, options } = await passkeyRegisterBegin();
      const credential = await startRegistration({ optionsJSON: options });
      return passkeyRegisterComplete({ handle, credential, name: "Face ID" });
    },
    onSuccess: () => { show("کلید عبور ثبت شد ✓", "success"); qc.invalidateQueries({ queryKey: ["passkeys"] }); },
    onError: (e: any) => {
      if (e?.name !== "NotAllowedError" && e?.name !== "AbortError") show(errMsg(e), "error");
    },
  });
  const delPasskey = useMutation({
    mutationFn: (id: number) => passkeyDelete(id),
    onSuccess: () => { show("کلید عبور حذف شد"); qc.invalidateQueries({ queryKey: ["passkeys"] }); },
    onError: (e) => show(errMsg(e), "error"),
  });

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
    onSuccess: (r: any) => {
      const ok = r?.status === "ok" || r?.status === "sent";  // backend returns "sent"
      show(ok ? "پشتیبان به تلگرام شما ارسال شد" : `وضعیت: ${r?.status}`, ok ? "success" : "info");
    },
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

  // ----- self-update -----
  const { data: info } = useQuery({ queryKey: ["info"], queryFn: getInfo });
  const [updating, setUpdating] = useState(false);
  const [updateMsg, setUpdateMsg] = useState("");
  const startedBefore = useRef<string>("");   // version at the moment we hit "update"

  const update = useMutation({
    mutationFn: () => updateSystem(),
    onSuccess: (r: any) => {
      startedBefore.current = info?.version || "";
      setUpdating(true);
      setUpdateMsg(r.message || "درخواست به‌روزرسانی ثبت شد…");
    },
    onError: (e) => show(errMsg(e), "error"),
  });

  // While updating, poll with a SHORT-timeout probe so the loop never hangs on the
  // default 120s when the backend stalls mid-rebuild. Completion = the backend is reachable
  // again AND its version changed from what we started on, OR it bounced (down→up). Both
  // independent of the status file. A manual «بارگذاری مجدد» button is always shown too, so
  // the owner is never stuck even if auto-detection misses.
  useEffect(() => {
    if (!updating) return;
    let stop = false;
    let sawDown = false;   // backend became unreachable (rebuild started)
    const startVer = startedBefore.current;
    const startedAt = Date.now();
    const MAX_MS = 10 * 60 * 1000;

    const doReload = () => {
      setUpdateMsg("✅ به‌روزرسانی انجام شد. در حال بارگذاری مجدد…");
      setTimeout(() => window.location.reload(), 1200);
    };

    const tick = async () => {
      if (stop) return;
      const elapsed = Date.now() - startedAt;
      try {
        // First, a cheap status read (best-effort) for nicer messaging + the "not installed"
        // warning. Don't let it block completion detection.
        let phase = "";
        try {
          const st = await getUpdateStatus();
          phase = st.phase || "";
          if (st.phase === "failed") {
            setUpdating(false);
            show("به‌روزرسانی ناموفق بود. گزارش را روی سرور (update/update.log) ببینید.", "error");
            return;
          }
          if (st.updater_installed === false && st.phase === "requested" && elapsed < 15000) {
            setUpdateMsg("⚠️ سرویس به‌روزرسانیِ خودکار روی سرور نصب نیست. لطفاً یک‌بار دستور نصب را روی سرور اجرا کنید تا فعال شود.");
          }
        } catch { /* ignore — covered by the probe below */ }

        // The decisive signal: probe the backend's live version with a short timeout.
        const { version } = await pingInfo(4000);
        if (stop) return;
        if (sawDown || (startVer && version && version !== startVer)) {
          doReload();
          return;
        }
        setUpdateMsg(
          phase === "running"
            ? "در حال دریافت آخرین نسخه و بازسازی… سامانه چند لحظه از دسترس خارج می‌شود."
            : "درخواست ثبت شد؛ در انتظار شروع و بازسازی…",
        );
      } catch {
        // Backend unreachable → it's bouncing for the rebuild.
        sawDown = true;
        setUpdateMsg("سامانه در حال بازسازی است… (چند لحظه از دسترس خارج می‌شود)");
      }
      if (!stop && elapsed > MAX_MS) {
        setUpdateMsg("به‌روزرسانی بیش از حد انتظار طول کشید. اگر سایت بالا آمده، دکمهٔ «بارگذاری مجدد» را بزنید.");
      }
      if (!stop) setTimeout(tick, 3000);
    };
    const id = setTimeout(tick, 3000);
    return () => { stop = true; clearTimeout(id); };
  }, [updating]);  // eslint-disable-line react-hooks/exhaustive-deps

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
            <TextField label="نام کاربری جدید (اختیاری)" value={newUser} inputProps={{ dir: "ltr" }}
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
                <img src={setup.qr} alt="کد QR تأیید دو مرحله‌ای" width={170} height={170}
                  style={{ border: "1px solid #e5e7eb", borderRadius: 8 }} />
                <Box>
                  <Typography variant="caption" color="text.secondary">کلید دستی:</Typography>
                  <Typography dir="ltr" sx={{ fontFamily: "monospace", wordBreak: "break-all", mb: 2 }}>{setup.secret}</Typography>
                  <TextField label="کد ۶ رقمی" value={code} inputProps={{ maxLength: 6, dir: "ltr" }}
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

      {/* Passkey / Face ID */}
      {passkeySupported && (
        <Card>
          <CardContent>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
              <FingerprintIcon color={passkeys.length ? "success" : "primary"} />
              <Typography variant="h6">ورود با Face ID / کلید عبور</Typography>
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              پس از ثبتِ این دستگاه، می‌توانید بدونِ رمز و کپچا فقط با Face ID / Touch ID وارد شوید.
              رمز عبور به‌عنوان پشتیبان باقی می‌ماند.
            </Typography>
            {passkeys.length > 0 && (
              <Stack spacing={1} sx={{ mb: 2 }}>
                {passkeys.map((k) => (
                  <Stack key={k.id} direction="row" alignItems="center" spacing={1}>
                    <FingerprintIcon fontSize="small" color="action" />
                    <Typography variant="body2" sx={{ flexGrow: 1 }}>{k.name || "کلید عبور"}</Typography>
                    <Button size="small" color="error" startIcon={<DeleteOutlineIcon />}
                      disabled={delPasskey.isPending} onClick={() => delPasskey.mutate(k.id)}>حذف</Button>
                  </Stack>
                ))}
              </Stack>
            )}
            <Button variant="contained" startIcon={<FingerprintIcon />}
              disabled={addPasskey.isPending} onClick={() => addPasskey.mutate()}>
              افزودن این دستگاه (Face ID)
            </Button>
          </CardContent>
        </Card>
      )}

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

      {/* Update to latest version */}
      <Card>
        <CardContent>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
            <SystemUpdateAltIcon color="primary" />
            <Typography variant="h6">به‌روزرسانی سامانه</Typography>
            {info?.version && <Chip size="small" label={`نسخهٔ فعلی: ${info.version}`} />}
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            با این دکمه، سامانه به آخرین نسخه به‌روزرسانی می‌شود (بدون نیاز به ترمینال سرور). دیتابیس شما حفظ می‌شود.
            هنگام به‌روزرسانی، سایت برای چند لحظه از دسترس خارج می‌شود و سپس صفحه به‌صورت خودکار دوباره بارگذاری می‌شود.
          </Typography>
          {updating ? (
            <Box>
              <LinearProgress sx={{ mb: 1 }} />
              <Typography variant="body2" sx={{ mb: 1.5 }}>{updateMsg}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                اگر بعد از چند دقیقه صفحه خودکار باز نشد، دکمهٔ زیر را بزنید.
              </Typography>
              <Button variant="outlined" startIcon={<RestartAltIcon />}
                onClick={() => window.location.reload()}>
                بارگذاری مجدد صفحه
              </Button>
            </Box>
          ) : (
            <Button variant="contained" startIcon={<SystemUpdateAltIcon />} disabled={update.isPending}
              onClick={() => { if (confirm("سامانه به آخرین نسخه به‌روزرسانی شود؟ سایت برای چند لحظه از دسترس خارج می‌شود.")) update.mutate(); }}>
              به‌روزرسانی به آخرین نسخه
            </Button>
          )}
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
            <TextField label="دامنه" inputProps={{ dir: "ltr" }} placeholder="panel.example.com"
              value={domain} onChange={(e) => setDomainVal(e.target.value)} />
            <TextField label="ایمیل برای گواهی SSL (اختیاری)" inputProps={{ dir: "ltr" }}
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
