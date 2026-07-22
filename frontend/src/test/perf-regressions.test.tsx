/**
 * Regression tests for the 2026-07-22 performance batch (v1.99.1).
 *
 * Locks in the three behavior guarantees the batch introduced:
 *  1. The shared QueryClient serves cached data across navigations (staleTime > 0,
 *     keepPreviousData) instead of refetch-on-every-mount.
 *  2. A returning visitor paints immediately: `setup_done` cached in localStorage skips
 *     the network-gated full-screen spinner (the status is still revalidated in the
 *     background and clears the cache if the install was reset).
 *  3. The axios client fails fast by default (20 s) while long owner operations keep
 *     explicit generous overrides — a stalled request must not pin a spinner for minutes.
 */
import { ThemeProvider } from "@mui/material";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../theme";
import { server } from "./setup";
import { queryClient } from "../api/queryClient";
import { api } from "../api/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "../auth/AuthContext";
import App from "../App";

describe("query cache defaults (instant warm navigation)", () => {
  it("keeps data fresh for 60s and renders previous data while a new key loads", () => {
    const d = queryClient.getDefaultOptions().queries!;
    expect(d.staleTime).toBe(60_000);
    expect(d.placeholderData).toBeTypeOf("function"); // keepPreviousData
    expect(d.refetchOnWindowFocus).toBe(false);
  });
});

describe("axios timeout policy (fail fast on stalled connections)", () => {
  it("defaults to 20s", () => {
    expect(api.defaults.timeout).toBe(20_000);
  });
});

describe("setup gate", () => {
  const renderApp = () =>
    render(
      <ThemeProvider theme={makeTheme("light")}>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={["/login"]}>
            <AuthProvider>
              <App />
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </ThemeProvider>,
    );

  it("paints the login screen immediately when setup_done is cached, without waiting for the network", async () => {
    localStorage.setItem("setup_done", "1");
    server.use(
      // A slow backend: if the app still gated first paint on this request the login
      // form could not appear before the response resolves.
      http.get("*/api/setup/status", async () => {
        await delay(3_000);
        return HttpResponse.json({ setup_done: true, domain: "", https_enabled: false });
      }),
      http.get("*/api/auth/captcha", () =>
        HttpResponse.json({ captcha_id: "c1", image: "data:image/png;base64," }),
      ),
    );
    renderApp();
    // Login page content is reachable NOW (no full-screen setup spinner).
    expect(await screen.findByText("ورود به سامانه", {}, { timeout: 1_500 })).toBeInTheDocument();
  });

  it("blocks on the status (spinner) on a truly fresh visit with no cache", async () => {
    server.use(
      http.get("*/api/setup/status", async () => {
        await delay(3_000);
        return HttpResponse.json({ setup_done: true, domain: "", https_enabled: false });
      }),
    );
    renderApp();
    expect(screen.queryByText("ورود به سامانه")).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });
});
