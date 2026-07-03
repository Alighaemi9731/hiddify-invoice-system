import { Snackbar, Alert, Button } from "@mui/material";
import { useRegisterSW } from "virtual:pwa-register/react";

/**
 * PWA update prompt. With `registerType: "prompt"` a freshly deployed service worker stays
 * WAITING instead of swapping assets mid-session; this toast tells the user a new version is
 * ready and, on tap, activates it and reloads. Mounted once (in main.tsx) so it covers both the
 * owner app and the reseller portal. Also re-checks for updates hourly while the tab is open.
 */
export default function UpdateToast() {
  const {
    needRefresh: [needRefresh, setNeedRefresh],
    updateServiceWorker,
  } = useRegisterSW({
    onRegisteredSW(_url, r) {
      if (r) setInterval(() => r.update().catch(() => {}), 60 * 60 * 1000);
    },
  });

  return (
    <Snackbar
      open={needRefresh}
      anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      sx={{ mb: { xs: "calc(64px + env(safe-area-inset-bottom))", md: 0 } }}
    >
      <Alert
        severity="info"
        variant="filled"
        onClose={() => setNeedRefresh(false)}
        action={
          <Button color="inherit" size="small" onClick={() => updateServiceWorker(true)}>
            بارگذاری مجدد
          </Button>
        }
      >
        نسخهٔ جدیدی از برنامه آماده است
      </Alert>
    </Snackbar>
  );
}
