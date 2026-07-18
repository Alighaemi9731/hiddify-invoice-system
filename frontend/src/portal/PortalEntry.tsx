import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Box, Button, Card, CardContent, CircularProgress, Stack, Typography } from "@mui/material";
import ReceiptLongIcon from "@mui/icons-material/esm/ReceiptLong";
import {
  portalAuthorizeNext,
  portalEntryConfig,
  portalExchange,
  portalTelegramLogin,
} from "./portalClient";
import { usePortalAuth } from "./PortalAuthContext";

// /portal/u/<uuid>[?next=<deep-link>] — the reseller's PERMANENT portal address.
//
// It replaced the 15-minute one-time link, which expired before people got round to tapping it
// (from the bot menu, or a «مشاهده در پنل» notification). Keeping it permanent is safe because the
// uuid is only an ADDRESS: it grants nothing on its own.
//   • already have a valid session → straight in (the session is 30 days and slides, so in practice
//     this is every time);
//   • otherwise → prove which Telegram account you are with Telegram's official login button. The
//     server then checks that account actually owns this uuid, so pasting someone else's uuid can
//     never open their portal.
export default function PortalEntry() {
  const { uuid = "" } = useParams();
  const [params] = useSearchParams();
  const nav = useNavigate();
  const { authed, finishLogin } = usePortalAuth();
  const [botUsername, setBotUsername] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const widgetBox = useRef<HTMLDivElement | null>(null);
  const ran = useRef(false);

  const goToDestination = async () => {
    let target = "/portal";
    try {
      const next = params.get("next");
      if (next) target = await portalAuthorizeNext(next);
    } catch {
      target = "/portal";
    }
    nav(target, { replace: true });
  };

  // Sign-in ladder, cheapest first:
  //   1. a `t=` from a link the bot just sent  → works instantly on any device;
  //   2. an existing 30-day session            → the common case, nothing to do;
  //   3. Telegram's login button               → new device / expired session.
  // A stale `t` silently falls through to 2/3 instead of erroring — that dead end was the whole
  // complaint about the old links.
  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const t = params.get("t");
    const showSignIn = () => {
      portalEntryConfig(uuid)
        .then((c) => setBotUsername(c.bot_username || ""))
        .catch(() => setBotUsername(""));
    };
    if (t) {
      // Strip the token from the address bar so it never lingers in history/referrer logs.
      window.history.replaceState({}, "", location.pathname + location.search.replace(/[?&]t=[^&]*/, "").replace(/^&/, "?"));
      setBusy(true);
      portalExchange(t)
        .then(async ({ access_token }) => {
          await finishLogin(access_token);
          await goToDestination();
        })
        .catch(() => {
          if (authed) void goToDestination();
          else showSignIn();
        })
        .finally(() => setBusy(false));
      return;
    }
    if (authed) {
      void goToDestination();
      return;
    }
    showSignIn();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Mount Telegram's official login button once we know the bot's @username.
  useEffect(() => {
    if (authed || !botUsername || !widgetBox.current) return;
    (window as any).onTelegramAuth = async (user: Record<string, unknown>) => {
      setBusy(true);
      setError(null);
      try {
        const { access_token } = await portalTelegramLogin(uuid, user);
        await finishLogin(access_token);
        await goToDestination();
      } catch (e: any) {
        setError(
          e?.response?.data?.detail ||
            "ورود ناموفق بود؛ دوباره تلاش کنید یا از دکمهٔ «پنل تحت وب» در ربات استفاده کنید."
        );
      } finally {
        setBusy(false);
      }
    };
    const s = document.createElement("script");
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.async = true;
    s.setAttribute("data-telegram-login", botUsername);
    s.setAttribute("data-size", "large");
    s.setAttribute("data-userpic", "false");
    s.setAttribute("data-radius", "10");
    s.setAttribute("data-onauth", "onTelegramAuth(user)");
    widgetBox.current.appendChild(s);
    return () => {
      delete (window as any).onTelegramAuth;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botUsername, authed]);

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

            {authed || busy ? (
              <Stack spacing={2} alignItems="center">
                <CircularProgress size={28} />
                <Typography color="text.secondary" sx={{ fontSize: 14 }}>
                  در حال ورود به سامانه…
                </Typography>
              </Stack>
            ) : (
              <Stack spacing={2} alignItems="center" sx={{ width: "100%" }}>
                <Typography color="text.secondary" sx={{ fontSize: 14.5, lineHeight: 1.9 }}>
                  برای ورود، با همان حساب تلگرامی که پنل به نامِ آن ثبت شده تأیید کنید.
                </Typography>
                {/* Telegram's official button (needs the bot's domain set once in BotFather). */}
                <Box ref={widgetBox} sx={{ minHeight: 48, display: "grid", placeItems: "center" }} />
                {botUsername === "" && (
                  <Typography color="text.secondary" sx={{ fontSize: 13 }}>
                    اگر دکمهٔ تلگرام نمایش داده نشد، از دکمهٔ «🌐 پنل تحت وب» در رباتِ تلگرام وارد شوید.
                  </Typography>
                )}
                {botUsername && (
                  <Button
                    size="small"
                    variant="text"
                    href={`https://t.me/${botUsername}`}
                    target="_blank"
                    rel="noopener"
                  >
                    ورود از طریق ربات
                  </Button>
                )}
                {error && (
                  <Typography color="error" sx={{ fontSize: 14, lineHeight: 1.9 }}>
                    {error}
                  </Typography>
                )}
              </Stack>
            )}
          </Stack>
        </CardContent>
      </Card>
    </Box>
  );
}
