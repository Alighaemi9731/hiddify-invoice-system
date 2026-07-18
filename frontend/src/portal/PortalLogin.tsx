import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Box, Card, CardContent, CircularProgress, Stack, Typography } from "@mui/material";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import { portalAuthorizeNext, portalExchange } from "./portalClient";
import { usePortalAuth } from "./PortalAuthContext";

const NEXT_KEY = "portal_next";

// /portal/login?t=<one-time-token>[&next=<deep-link>] — exchange the bot-issued link for a reseller
// session, then land on the (server-authorized) deep-link target. If the token is missing/expired,
// tell them to re-tap the bot button (resellers have no password — the bot link is the credential).
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
    const nextRaw = params.get("next");
    // Stash `next`, then strip BOTH the token and next from the address bar immediately so neither
    // lingers in history/referrer logs. The server re-validates + authorizes `next` before we use it.
    if (t) {
      sessionStorage.setItem(NEXT_KEY, nextRaw ?? "");
      window.history.replaceState({}, "", "/portal/login");
    }

    // Resolve the safe destination server-side, then navigate (defaults to the dashboard).
    const goToDestination = async () => {
      const stashed = sessionStorage.getItem(NEXT_KEY);
      sessionStorage.removeItem(NEXT_KEY);
      let target = "/portal";
      try {
        if (stashed) target = await portalAuthorizeNext(stashed);
      } catch {
        target = "/portal";
      }
      nav(target, { replace: true });
    };

    if (!t) {
      // No token in the URL: if already logged in, honor any stashed next; otherwise prompt to use the bot.
      if (authed) void goToDestination();
      else setError("برای ورود، از دکمهٔ «ورود به پنلِ تحتِ وب» در ربات تلگرام استفاده کنید.");
      return;
    }
    portalExchange(t)
      .then(async ({ access_token }) => {
        await finishLogin(access_token);
        await goToDestination();
      })
      .catch((e) => {
        // The one-time token is dead (expired or already used) — but that must not be a dead end.
        // These links are frequently tapped hours later (an old menu message, a support
        // notification), and the reseller usually still holds a valid 30-day session, so just carry
        // on to where they were going instead of showing "منقضی شده".
        if (authed) {
          void goToDestination();
          return;
        }
        sessionStorage.removeItem(NEXT_KEY);
        setError(
          e?.response?.data?.detail ||
            "لینکِ ورود منقضی شده است؛ از دکمهٔ «🌐 پنل تحت وب» در ربات تلگرام وارد شوید."
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
