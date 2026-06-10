import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Box,
  Button,
  Divider,
  IconButton,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import FingerprintIcon from "@mui/icons-material/esm/Fingerprint";
import LockRoundedIcon from "@mui/icons-material/esm/LockRounded";
import ReceiptLongRoundedIcon from "@mui/icons-material/esm/ReceiptLongRounded";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import VisibilityOffOutlinedIcon from "@mui/icons-material/esm/VisibilityOffOutlined";
import VisibilityOutlinedIcon from "@mui/icons-material/esm/VisibilityOutlined";
import { startAuthentication } from "@simplewebauthn/browser";
import { useAuth } from "../auth/AuthContext";
import { getCaptcha, login, passkeyLoginBegin, passkeyLoginComplete } from "../api/client";

const passkeySupported =
  typeof window !== "undefined" && !!window.PublicKeyCredential;

const fieldSx = {
  "& .MuiOutlinedInput-root": {
    height: 58,
    color: "#172033",
    borderRadius: "14px",
    backgroundColor: "#fff",
    transition: "box-shadow .2s ease, border-color .2s ease",
    "& fieldset": { borderColor: "#d6d9df", borderWidth: 1 },
    "&:hover fieldset": { borderColor: "#aeb5c1" },
    "&.Mui-focused": {
      boxShadow: "0 0 0 3px rgba(12, 52, 82, .08)",
    },
    "&.Mui-focused fieldset": { borderColor: "#526275", borderWidth: 1 },
  },
  "& input": {
    px: 2,
    direction: "rtl",
  },
};

const rtlInputProps = {
  dir: "rtl" as const,
  style: { textAlign: "right" as const },
};

export default function Login() {
  const { finishLogin, authed } = useAuth();
  const nav = useNavigate();
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
    } catch {
      // The form remains usable while the backend reconnects.
    }
  };

  useEffect(() => { loadCaptcha(); }, []);
  useEffect(() => {
    if (authed) nav("/", { replace: true });
  }, [authed, nav]);

  const loginWithPasskey = async () => {
    setErr("");
    setBusy(true);
    try {
      const { handle, options } = await passkeyLoginBegin();
      const credential = await startAuthentication({ optionsJSON: options });
      const { access_token } = await passkeyLoginComplete({ handle, credential });
      await finishLogin(access_token);
      nav("/", { replace: true });
    } catch (e: any) {
      if (e?.name !== "NotAllowedError" && e?.name !== "AbortError") {
        setErr(e?.response?.data?.detail || "ورود با Face ID ناموفق بود. با رمز وارد شوید.");
      }
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const { access_token } = await login({
        username: u,
        password: p,
        captcha_id: captchaId,
        captcha_answer: captchaAns,
        totp_code: need2fa ? totp : undefined,
      });
      await finishLogin(access_token);
      nav("/", { replace: true });
    } catch (e: any) {
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail;
      if (status === 401 && e?.response?.headers?.["x-2fa-required"]) {
        setNeed2fa(true);
        await loadCaptcha();
        setErr("کد تأیید دو مرحله‌ای و کد امنیتیِ جدید را وارد کنید.");
      } else {
        setErr(detail || "ورود ناموفق بود.");
        await loadCaptcha();
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box
      dir="ltr"
      sx={{
        minHeight: "100dvh",
        display: "grid",
        gridTemplateColumns: { xs: "minmax(0, 1fr)", md: "46% 54%" },
        overflow: "hidden",
        position: "relative",
        backgroundColor: "#fff",
        "&::before": {
          content: '""',
          display: { xs: "none", md: "block" },
          position: "absolute",
          inset: 0,
          background: [
            "radial-gradient(ellipse 94% 145% at 100% 50%, rgba(184, 217, 255, .82) 0%, rgba(211, 232, 255, .64) 48%, rgba(239, 247, 255, .28) 76%, rgba(255, 255, 255, 0) 100%)",
            "linear-gradient(90deg, rgba(255, 255, 255, 0) 34%, rgba(242, 248, 255, .28) 48%, rgba(224, 239, 255, .58) 70%, rgba(198, 225, 255, .78) 100%)",
          ].join(", "),
          pointerEvents: "none",
        },
      }}
    >
      <Box
        component="main"
        dir="rtl"
        sx={{
          minHeight: "100dvh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          px: { xs: 2.5, sm: 6, md: 5 },
          py: { xs: 4, sm: 6 },
          backgroundColor: "transparent",
          position: "relative",
          zIndex: 2,
          minWidth: 0,
        }}
      >
        <Box dir="rtl" sx={{ width: "100%", maxWidth: 600, minWidth: 0 }}>
          <Typography
            component="h1"
            style={{ textAlign: "right" }}
            sx={{
              color: "#151b25",
              fontSize: { xs: 23, sm: 25 },
              fontWeight: 800,
              mb: { xs: 3.5, sm: 5 },
            }}
          >
            ورود
          </Typography>

          <form onSubmit={submit}>
            {err && (
              <Alert
                severity={need2fa ? "info" : "error"}
                sx={{
                  mb: 2,
                  borderRadius: "12px",
                  fontSize: 13,
                  "& .MuiAlert-message": { py: .25 },
                }}
              >
                {err}
              </Alert>
            )}

            <Stack spacing={2}>
              <TextField
                placeholder="نام کاربری"
                fullWidth
                value={u}
                name="username"
                autoComplete="username"
                onChange={(e) => setU(e.target.value)}
                autoFocus
                inputProps={rtlInputProps}
                sx={fieldSx}
              />

              <TextField
                placeholder="رمز عبور"
                type={showPassword ? "text" : "password"}
                fullWidth
                value={p}
                name="password"
                autoComplete="current-password"
                onChange={(e) => setP(e.target.value)}
                sx={fieldSx}
                inputProps={rtlInputProps}
                InputProps={{
                  endAdornment: (
                    <IconButton
                      aria-label={showPassword ? "پنهان کردن رمز عبور" : "نمایش رمز عبور"}
                      onClick={() => setShowPassword((value) => !value)}
                      edge="end"
                      sx={{ color: "#647083" }}
                    >
                      {showPassword ? <VisibilityOutlinedIcon /> : <VisibilityOffOutlinedIcon />}
                    </IconButton>
                  ),
                }}
              />

              <Stack direction="row" spacing={1} alignItems="stretch">
                <TextField
                  placeholder="کد امنیتی"
                  fullWidth
                  value={captchaAns}
                  autoComplete="off"
                  onChange={(e) => setCaptchaAns(e.target.value)}
                  inputProps={{ ...rtlInputProps, maxLength: 6 }}
                  sx={fieldSx}
                />
                <Box
                  sx={{
                    width: { xs: 106, sm: 132 },
                    height: 58,
                    flex: "0 0 auto",
                    display: "grid",
                    placeItems: "center",
                    overflow: "hidden",
                    border: "1px solid #d6d9df",
                    borderRadius: "14px",
                    backgroundColor: "#f7f9fb",
                    "& img": {
                      display: "block",
                      width: "100%",
                      height: "100%",
                      objectFit: "contain",
                    },
                  }}
                >
                  {captchaImg && <img src={captchaImg} alt="تصویر کد امنیتی" />}
                </Box>
                <Tooltip title="کد جدید">
                  <IconButton
                    aria-label="دریافت کد امنیتی جدید"
                    onClick={loadCaptcha}
                    sx={{
                      width: 48,
                      height: 58,
                      flex: "0 0 auto",
                      border: "1px solid #d6d9df",
                      borderRadius: "14px",
                      color: "#48566a",
                      "&:hover": { bgcolor: "#f4f7fa" },
                    }}
                  >
                    <RefreshIcon />
                  </IconButton>
                </Tooltip>
              </Stack>

              {need2fa && (
                <TextField
                  placeholder="کد تأیید دو مرحله‌ای"
                  fullWidth
                  value={totp}
                  autoComplete="one-time-code"
                  onChange={(e) => setTotp(e.target.value)}
                  inputProps={{ ...rtlInputProps, maxLength: 6 }}
                  autoFocus
                  sx={fieldSx}
                />
              )}

              <Button
                type="submit"
                variant="contained"
                fullWidth
                size="large"
                disabled={busy}
                sx={{
                  minHeight: 54,
                  mt: "10px !important",
                  borderRadius: "14px",
                  bgcolor: "#05263d",
                  color: "#fff",
                  fontSize: 15.5,
                  fontWeight: 500,
                  boxShadow: "none",
                  "&:hover": {
                    bgcolor: "#0a354f",
                    boxShadow: "0 8px 20px rgba(5,38,61,.16)",
                  },
                }}
              >
                {busy ? "در حال ورود..." : "ورود به سامانه"}
              </Button>
            </Stack>

            {passkeySupported && (
              <>
                <Divider
                  sx={{
                    my: 2.25,
                    color: "#9aa1ab",
                    fontSize: 11.5,
                    "&::before, &::after": { borderColor: "#e3e5e9" },
                  }}
                >
                  یا
                </Divider>
                <Button
                  fullWidth
                  size="large"
                  variant="text"
                  startIcon={<FingerprintIcon />}
                  onClick={loginWithPasskey}
                  disabled={busy}
                  sx={{
                    minHeight: 46,
                    borderRadius: "12px",
                    color: "#0a3049",
                    fontSize: 14,
                    "&:hover": { bgcolor: "#f2f6f9" },
                  }}
                >
                  ورود با Face ID / کلید عبور
                </Button>
              </>
            )}
          </form>

          <Stack direction="row" spacing={.7} alignItems="center" justifyContent="center" sx={{ mt: 3.5 }}>
            <LockRoundedIcon sx={{ fontSize: 14, color: "#a2a8b1" }} />
            <Typography sx={{ color: "#969da7", fontSize: 11.5 }}>
              ورود امن و رمزگذاری‌شده
            </Typography>
          </Stack>
        </Box>
      </Box>

      <Box
        component="aside"
        aria-hidden="true"
        sx={{
          minHeight: "100dvh",
          display: { xs: "none", md: "block" },
          position: "relative",
          overflow: "hidden",
          backgroundColor: "transparent",
        }}
      >
        <Stack
          direction="row"
          alignItems="center"
          spacing={1}
          sx={{
            position: "absolute",
            zIndex: 2,
            top: { md: 52, lg: 68 },
            insetInlineEnd: { md: 46, lg: 70 },
          }}
        >
          <Box
            sx={{
              width: 40,
              height: 40,
              display: "grid",
              placeItems: "center",
              borderRadius: "10px",
              color: "#fff",
              bgcolor: "#082b43",
              transform: "rotate(-4deg)",
            }}
          >
            <ReceiptLongRoundedIcon sx={{ fontSize: 23 }} />
          </Box>
          <Typography sx={{ color: "#092b43", fontSize: 19, fontWeight: 850 }}>
            سامانه فاکتور
          </Typography>
        </Stack>

        <Box
          component="img"
          src="/login.svg"
          alt=""
          sx={{
            position: "absolute",
            width: { md: "90%", lg: "88%" },
            maxWidth: 900,
            insetInlineEnd: { md: "4%", lg: "5%" },
            top: "55%",
            transform: "translateY(-50%)",
            objectFit: "contain",
            filter: "drop-shadow(0 28px 30px rgba(35,69,108,.14))",
          }}
        />
      </Box>
    </Box>
  );
}
