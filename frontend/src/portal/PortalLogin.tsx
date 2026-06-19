import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Box, Card, CardContent, CircularProgress, Stack, Typography } from "@mui/material";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import { portalExchange } from "./portalClient";
import { usePortalAuth } from "./PortalAuthContext";

// /portal/login?t=<one-time-token> — exchange the bot-issued link for a reseller session,
// then redirect into the portal. If the token is missing/expired, tell them to re-tap the
// bot button (resellers have no password — the bot link is the credential).
export default function PortalLogin() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const { authed, finishLogin } = usePortalAuth();
  const [error, setError] = useState<string | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const t = params.get("t");
    if (!t) {
      // No token in the URL: if already logged in, go in; otherwise prompt to use the bot.
      if (authed) nav("/portal", { replace: true });
      else setError("برای ورود، از دکمهٔ «ورود به پنلِ تحتِ وب» در ربات تلگرام استفاده کنید.");
      return;
    }
    portalExchange(t)
      .then(async ({ access_token }) => {
        await finishLogin(access_token);
        nav("/portal", { replace: true });
      })
      .catch((e) => {
        setError(
          e?.response?.data?.detail ||
            "لینکِ ورود نامعتبر یا منقضی شده است؛ از ربات تلگرام دوباره وارد شوید."
        );
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Box
      sx={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        p: 2,
        background:
          "radial-gradient(ellipse 80% 60% at 70% 0%, rgba(0,113,227,.14) 0%, transparent 60%)",
      }}
    >
      <Card sx={{ width: "100%", maxWidth: 420 }}>
        <CardContent sx={{ p: { xs: 3, sm: 4 } }}>
          <Stack spacing={2.5} alignItems="center" textAlign="center">
            <Box
              sx={{
                width: 56, height: 56, borderRadius: 3, color: "#fff",
                display: "grid", placeItems: "center",
                background: "linear-gradient(145deg, #5ab5ff 0%, #0071e3 100%)",
                boxShadow: "0 8px 22px -8px rgba(0,113,227,.6)",
              }}
            >
              <ReceiptLongIcon />
            </Box>
            <Typography variant="h6" sx={{ fontWeight: 800 }}>
              پنلِ نمایندگان
            </Typography>

            {error ? (
              <Typography color="error" sx={{ fontSize: 14.5, lineHeight: 1.9 }}>
                {error}
              </Typography>
            ) : (
              <Stack spacing={2} alignItems="center">
                <CircularProgress size={28} />
                <Typography color="text.secondary" sx={{ fontSize: 14 }}>
                  در حال ورود به سامانه…
                </Typography>
              </Stack>
            )}
          </Stack>
        </CardContent>
      </Card>
    </Box>
  );
}
