import { ReactElement } from "react";
import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeTheme } from "../../../theme";
import { server, setMatchMedia } from "../../../test/setup";
import { PortalAuthProvider } from "../../PortalAuthContext";
import PortalLayout from "../../PortalLayout";
import StorefrontDashboardPage from "../StorefrontDashboardPage";
import StorefrontHealthPanel from "../StorefrontHealthPanel";
import StorefrontIndexPage from "../StorefrontIndexPage";
import StorefrontShell from "../StorefrontShell";

vi.mock("../../../components/EChart", () => ({
  default: ({ ariaLabel }: { ariaLabel?: string }) => <div role="img" aria-label={ariaLabel || "نمودار"} />,
}));

const shop = (id: number, name = `فروشگاه ${id}`) => ({
  id,
  reseller: { id: id * 10, name },
  panel: { id: id * 100, key: `panel-${id}` },
  bot_username: `shop_${id}_bot`,
  enabled: true,
  status: "active",
  health_error_class: null,
  health_state_updated_at: null,
  shop_closed: false,
  role: "owner",
});

const sales = (net: number) => ({
  gross_sales_toman: net + 50_000,
  reversals_toman: 50_000,
  net_sales_toman: net,
  purchase: { count: 3, amount_toman: net - 200_000 },
  renewal: { count: 2, amount_toman: 150_000 },
  unknown: { count: 1, amount_toman: 50_000 },
});

const dashboard = (id: number) => ({
  storefront_id: id,
  range: { from_date: "2026-07-01", to_date: "2026-07-16", timezone: "Asia/Tehran" },
  sales_today: sales(210_000),
  sales_month: sales(1_250_000),
  sales_range: sales(1_250_000),
  customers: { total: 42, active_30d: 31, wallet_liability_toman: 900_000 },
  service_states: { pending: 1, renewing: 2, provisioned: 28, disabled: 4, failed: 2, deleted: 7 },
  near_expiry: 3,
  pending_topups: { count: 2, amount_toman: 300_000 },
  credits: { redemptions: 8, bonus_toman: 80_000 },
  operation_states: { pending: 1, in_progress: 0, done: 35, failed: 2, reversed: 1 },
  trial_conversion: { trial_customers: 8, converted_customers: 3, rate: 0.375 },
});

const health = (id: number, errorClass: string | null = null, botStatus = "active", panelStatus = "ok") => ({
  storefront_id: id,
  bot: { enabled: true, status: botStatus, error_class: errorClass, state_updated_at: "2026-07-16T08:00:00Z" },
  panel: { id: id * 100, key: `panel-${id}`, enabled: true, status: panelStatus, last_synced_at: "2026-07-16T08:05:00Z", error_class: null, state_updated_at: "2026-07-16T08:01:00Z" },
  operation_states: { pending: 1, in_progress: 0, done: 35, failed: 2, reversed: 1 },
});

function renderStorefront(initialPath: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/portal/storefront" element={<StorefrontIndexPage />} />
      <Route path="/portal/storefront/:shopId" element={<StorefrontShell />}>
        <Route index element={<StorefrontDashboardPage />} />
        <Route path="health" element={<StorefrontHealthPanel />} />
      </Route>
    </Routes>,
    initialPath,
  );
}

function renderWithProviders(element: ReactElement, initialPath = "/portal/storefront") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialPath]}>{element}</MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("storefront portal", () => {
  it("redirects a single shop to its URL-scoped dashboard and renders metrics", async () => {
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1, "فروشگاه اصلی")])),
      http.get("*/api/portal/storefronts/1/dashboard", () => HttpResponse.json(dashboard(1))),
    );

    renderStorefront("/portal/storefront");

    expect(await screen.findByText("فروشگاه اصلی")).toBeInTheDocument();
    expect(await screen.findByText("۲۱۰٬۰۰۰ تومان")).toBeInTheDocument();
    expect(screen.getByText("۳۷٫۵٪")).toBeInTheDocument();
    expect(screen.getByText(/۳ تبدیل از ۸ مشتری آزمایشی/)).toBeInTheDocument();
    expect(screen.getByText("در حال تمدید: ۲")).toBeInTheDocument();
    // The old chart merely re-drew the three numbers printed beneath it; the dashboard now leads
    // with a real daily sales trend and a best-selling-plans ranking.
    expect(screen.getByText("روند فروش روزانه")).toBeInTheDocument();
    expect(screen.getByText(/پرفروش‌ترین پلن‌ها/)).toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: "نمودار مبلغ خرید، تمدید و فروش قدیمی دوره" })
    ).not.toBeInTheDocument();
  });

  it("lets an owner switch between multiple shops while retaining selection in the route", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1, "فروشگاه تهران"), shop(2, "فروشگاه شیراز")])),
      http.get("*/api/portal/storefronts/:shopId/dashboard", ({ params }) =>
        HttpResponse.json(dashboard(Number(params.shopId)))),
    );

    renderStorefront("/portal/storefront/1");

    expect(await screen.findByRole("heading", { name: "فروشگاه تهران" })).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "انتخاب فروشگاه" }));
    await user.click(await screen.findByRole("option", { name: "فروشگاه شیراز" }));
    expect(await screen.findByRole("heading", { name: "فروشگاه شیراز" })).toBeInTheDocument();
  });

  it("shows a clear empty state when the owner has no configured shop", async () => {
    server.use(http.get("*/api/portal/storefronts", () => HttpResponse.json([])));

    renderStorefront("/portal/storefront");

    expect(await screen.findByText(/هنوز فروشگاهی برای این حساب فعال نشده است/)).toBeInTheDocument();
  });

  it("does not request dashboard data for a foreign or absent shop id", async () => {
    let dashboardCalls = 0;
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/storefronts/99/dashboard", () => {
        dashboardCalls += 1;
        return HttpResponse.json({}, { status: 404 });
      }),
    );

    renderStorefront("/portal/storefront/99");

    expect(await screen.findByText(/این فروشگاه پیدا نشد/)).toBeInTheDocument();
    expect(dashboardCalls).toBe(0);
  });

  it("renders loading and recovers from a discovery error", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(http.get("*/api/portal/storefronts", async () => {
      calls += 1;
      if (calls === 1) {
        await delay(40);
        return HttpResponse.json({ detail: "temporary" }, { status: 503 });
      }
      return HttpResponse.json([]);
    }));

    const { container } = renderStorefront("/portal/storefront");
    expect(container.querySelector(".MuiSkeleton-root")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /تلاش دوباره/ }));
    expect(await screen.findByText(/هنوز فروشگاهی برای این حساب فعال نشده است/)).toBeInTheDocument();
  });

  it("renders persisted health and sanitized error classes", async () => {
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/storefronts/1/health", () => HttpResponse.json(health(1, "network"))),
    );

    renderStorefront("/portal/storefront/1/health");

    // A sanitized, ACTIONABLE message — never the raw error text, and no owner-infrastructure
    // details (panel status/last-sync were removed: the reseller can do nothing about them).
    expect(await screen.findByText(/ارتباط با سرویس برقرار نیست/)).toBeInTheDocument();
    expect(screen.queryByText("آخرین همگام‌سازی پنل")).not.toBeInTheDocument();
    // Failed provisioning attempts stay visible because they DO affect the reseller's customers.
    expect(screen.getByText("۲")).toBeInTheDocument();
  });

  it("warns when persisted component statuses are unhealthy without an error class", async () => {
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/storefronts/1/health", () => HttpResponse.json(health(1, null, "stopped", "ok"))),
    );

    renderStorefront("/portal/storefront/1/health");

    expect(await screen.findByText(/ربات فروشگاه فعال نیست/)).toBeInTheDocument();
    expect(screen.queryByText(/فروشگاه شما سالم است/)).not.toBeInTheDocument();
    // Every chip is a translated Persian label — a raw backend value like "stopped"/"ok" must
    // never reach the UI (that mismatch is what made this panel look broken).
    expect(screen.queryByText("stopped")).not.toBeInTheDocument();
    expect(screen.queryByText("ok")).not.toBeInTheDocument();
  });

  it("shows a green health state only for the exact healthy persisted statuses", async () => {
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/storefronts/1/health", () => HttpResponse.json(health(1))),
    );

    renderStorefront("/portal/storefront/1/health");

    expect(
      await screen.findByText(/فروشگاه شما سالم است و سفارش‌ها به‌درستی انجام می‌شوند/)
    ).toBeInTheDocument();
    expect(screen.queryByText("active")).not.toBeInTheDocument();
  });

  it("exposes the storefront entry in the responsive portal menu only when a shop exists", async () => {
    const user = userEvent.setup();
    localStorage.setItem("portal_token", "test-token");
    server.use(
      http.get("*/api/portal/me", () => HttpResponse.json({
        chat_id: 123,
        resellers: [{ id: 10, name: "نماینده", admin_uuid: "a", panel_key: "panel-1", link_tag: null, enforcement_state: "active" }],
      })),
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/notifications", () => HttpResponse.json({ events: [] })),
    );

    renderWithProviders(
      <PortalAuthProvider>
        <Routes>
          <Route element={<PortalLayout />}>
            <Route path="/portal" element={<Outlet />} />
          </Route>
        </Routes>
      </PortalAuthProvider>,
      "/portal",
    );

    await user.click(screen.getByRole("button", { name: "باز کردن منو" }));
    expect(await screen.findByText("فروشگاه من")).toBeVisible();
    await waitFor(() => expect(screen.getByText("پنلِ نماینده")).toBeVisible());
  });

  it("hides the storefront navigation entry when discovery returns no shops", async () => {
    const user = userEvent.setup();
    let discoveryCompleted = false;
    localStorage.setItem("portal_token", "test-token");
    server.use(
      http.get("*/api/portal/me", () => HttpResponse.json({
        chat_id: 123,
        resellers: [{ id: 10, name: "نماینده", admin_uuid: "a", panel_key: "panel-1", link_tag: null, enforcement_state: "active" }],
      })),
      http.get("*/api/portal/storefronts", () => {
        discoveryCompleted = true;
        return HttpResponse.json([]);
      }),
      http.get("*/api/portal/notifications", () => HttpResponse.json({ events: [] })),
    );

    renderWithProviders(
      <PortalAuthProvider>
        <Routes>
          <Route element={<PortalLayout />}>
            <Route path="/portal" element={<Outlet />} />
          </Route>
        </Routes>
      </PortalAuthProvider>,
      "/portal",
    );

    await waitFor(() => expect(discoveryCompleted).toBe(true));
    await user.click(screen.getByRole("button", { name: "باز کردن منو" }));
    expect(screen.queryByText("فروشگاه من")).not.toBeInTheDocument();
  });

  it("renders the storefront navigation and page title in the desktop layout", async () => {
    setMatchMedia((query) => query.includes("min-width:900px"));
    localStorage.setItem("portal_token", "test-token");
    server.use(
      http.get("*/api/portal/me", () => HttpResponse.json({
        chat_id: 123,
        resellers: [{ id: 10, name: "نماینده", admin_uuid: "a", panel_key: "panel-1", link_tag: null, enforcement_state: "active" }],
      })),
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/notifications", () => HttpResponse.json({ events: [] })),
    );

    renderWithProviders(
      <PortalAuthProvider>
        <Routes>
          <Route element={<PortalLayout />}>
            <Route path="/portal/storefront/:shopId" element={<div>محتوای فروشگاه</div>} />
          </Route>
        </Routes>
      </PortalAuthProvider>,
      "/portal/storefront/1",
    );

    expect(await screen.findByRole("heading", { name: "فروشگاه من" })).toBeVisible();
    await waitFor(() => expect(screen.getByText("محتوای فروشگاه")).toBeVisible());
    expect(screen.queryByRole("button", { name: "باز کردن منو" })).not.toBeInTheDocument();
  });
});
