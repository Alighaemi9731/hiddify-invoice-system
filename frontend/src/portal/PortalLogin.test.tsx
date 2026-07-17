import { ThemeProvider } from "@mui/material";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeTheme } from "../theme";
import { server } from "../test/setup";
import PortalLogin from "./PortalLogin";
import { PortalAuthProvider } from "./PortalAuthContext";

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

function renderAt(entry: string) {
  return render(
    <ThemeProvider theme={makeTheme("light")}>
      <MemoryRouter initialEntries={[entry]}>
        <PortalAuthProvider>
          <Routes>
            <Route path="/portal/login" element={<PortalLogin />} />
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </PortalAuthProvider>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

function stubAuth(target: string) {
  server.use(
    http.post("*/api/portal/auth/exchange", () => HttpResponse.json({ access_token: "sess-token" })),
    http.get("*/api/portal/me", () =>
      HttpResponse.json({ chat_id: 111, resellers: [{ id: 1, name: "Alpha", admin_uuid: "a1", panel_key: "p1", link_tag: null, enforcement_state: "active" }] })),
    http.post("*/api/portal/authorize-next", () => HttpResponse.json({ target })),
  );
}

describe("PortalLogin deep-link", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("navigates to the server-authorized owned next", async () => {
    stubAuth("/portal/storefront/1/customers/5");
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    renderAt("/portal/login?t=TOK&next=%2Fportal%2Fstorefront%2F1%2Fcustomers%2F5");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/portal/storefront/1/customers/5"));
    // The address bar was stripped of BOTH the token and next immediately.
    expect(replaceSpy).toHaveBeenCalledWith({}, "", "/portal/login");
  });

  it("falls back to the dashboard when the server rejects a foreign/invalid next (no leak)", async () => {
    stubAuth("/portal/storefront");   // server degrades foreign/invalid to the dashboard
    renderAt("/portal/login?t=TOK&next=%2Fportal%2Fstorefront%2F999%2Ftopups");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/portal/storefront"));
  });

  it("shows a safe re-link message when the one-time token is expired/replayed", async () => {
    server.use(http.post("*/api/portal/auth/exchange", () =>
      HttpResponse.json(
        { detail: "این لینکِ ورود قبلاً استفاده شده است؛ از ربات یک لینکِ تازه بگیرید." },
        { status: 401 })));
    renderAt("/portal/login?t=USED&next=%2Fportal%2Fstorefront%2F1");
    expect(await screen.findByText(/از ربات/)).toBeInTheDocument();
    // A rejected exchange must not leave a stashed next behind.
    expect(sessionStorage.getItem("portal_next")).toBeNull();
  });
});
