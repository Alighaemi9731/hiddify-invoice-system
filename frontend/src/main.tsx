import React, { useMemo, useState } from "react";
import ReactDOM from "react-dom/client";
import { CacheProvider } from "@emotion/react";
import { ThemeProvider, CssBaseline, PaletteMode } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { rtlCache } from "./rtlCache";
import { makeTheme } from "./theme";
import { ColorModeContext } from "./colorMode";
import { AuthProvider } from "./auth/AuthContext";
import App from "./App";

const qc = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
});

function Root() {
  const [mode, setMode] = useState<PaletteMode>(
    () => (localStorage.getItem("color_mode") as PaletteMode) || "light"
  );
  const theme = useMemo(() => makeTheme(mode), [mode]);
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
          <QueryClientProvider client={qc}>
            <BrowserRouter>
              <AuthProvider>
                <App />
              </AuthProvider>
            </BrowserRouter>
          </QueryClientProvider>
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
