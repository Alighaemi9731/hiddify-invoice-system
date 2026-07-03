import React, { useEffect, useMemo, useState } from "react";
import ReactDOM from "react-dom/client";
import { CacheProvider } from "@emotion/react";
import { ThemeProvider, CssBaseline, PaletteMode } from "@mui/material";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { rtlCache } from "./rtlCache";
import { makeTheme } from "./theme";
import { ColorModeContext } from "./colorMode";
import { AuthProvider } from "./auth/AuthContext";
import { queryClient } from "./api/queryClient";
import UpdateToast from "./components/UpdateToast";
import App from "./App";

function Root() {
  const [mode, setMode] = useState<PaletteMode>(() => {
    const saved = localStorage.getItem("color_mode") as PaletteMode | null;
    if (saved) return saved; // an explicit choice always wins
    // First visit: follow the OS preference (the toggle persists any later change).
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  const theme = useMemo(() => makeTheme(mode), [mode]);
  // Keep the browser/OS chrome color (address bar, iOS status bar) in step with the manual
  // light/dark toggle. The media-based <meta> tags in index.html cover first paint / OS setting.
  useEffect(() => {
    document
      .querySelector('meta[name="theme-color"]:not([media])')
      ?.setAttribute("content", mode === "dark" ? "#000000" : "#f5f5f7");
  }, [mode]);
  const ctx = useMemo(
    () => ({
      mode,
      toggle: () =>
        setMode((m) => {
          const next = m === "light" ? "dark" : "light";
          localStorage.setItem("color_mode", next);
          return next;
        }),
    }),
    [mode]
  );

  return (
    <CacheProvider value={rtlCache}>
      <ColorModeContext.Provider value={ctx}>
        <ThemeProvider theme={theme}>
          <CssBaseline />
          <QueryClientProvider client={queryClient}>
            <BrowserRouter>
              <AuthProvider>
                <App />
              </AuthProvider>
            </BrowserRouter>
          </QueryClientProvider>
          <UpdateToast />
        </ThemeProvider>
      </ColorModeContext.Provider>
    </CacheProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
