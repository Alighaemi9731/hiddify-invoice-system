import { ReactElement } from "react";
import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeTheme } from "../../../theme";
import { server } from "../../../test/setup";
import StorefrontFinancePage from "../StorefrontFinancePage";
import StorefrontShell from "../StorefrontShell";

vi.mock("../../../components/EChart", () => ({
  default: ({ ariaLabel }: { ariaLabel?: string }) => <div role="img" aria-label={ariaLabel || "نمودار"} />,
}));

const shop = (id: number) => ({
  id,
  reseller: { id: id * 10, name: `فروشگاه ${id}` },
  panel: { id: id * 100, key: `panel-${id}` },
  bot_username: `shop_${id}_bot`,
  enabled: true,
  status: "active",
  health_error_class: null,
  health_state_updated_at: null,
  shop_closed: false,
  cost_per_gb_toman: 2000,
  role: "owner",
});

const dashboard = (id: number) => ({
  storefront_id: id,
  range: { from_date: "2026-07-01", to_date: "2026-07-16", timezone: "Asia/Tehran" },
  sales_today: null,
  sales_month: null,
  sales_range: null,
  customers: { total: 0, active_30d: 0, wallet_liability_toman: 0 },
  service_states: {},
  near_expiry: 0,
  pending_topups: { count: 0, amount_toman: 0 },
  credits: { redemptions: 0, bonus_toman: 0 },
  operation_states: {},
  trial_conversion: { trial_customers: 0, converted_customers: 0, rate: null },
});

const period = (label: string, over: Record<string, number> = {}) => ({
  label,
  purchases: 0,
  renewals: 0,
  gb_sold: 0,
  gb_free: 0,
  gb_billable: 0,
  cost_toman: 0,
  gross_sales_toman: 0,
  reversals_toman: 0,
  net_sales_toman: 0,
  profit_toman: 0,
  unresolved_ops: 0,
  ...over,
});

const finance = () => ({
  cost_per_gb_toman: 2000,
  excluded_below_gb: 1,
  months: [
    period("2026-07", {
      purchases: 4, renewals: 2, gb_sold: 120, gb_free: 20, gb_billable: 100,
      cost_toman: 200_000, gross_sales_toman: 500_000, net_sales_toman: 500_000,
      profit_toman: 300_000,
    }),
    period("2026-06", {
      purchases: 2, renewals: 1, gb_sold: 50, gb_billable: 50,
      cost_toman: 100_000, gross_sales_toman: 180_000, net_sales_toman: 180_000,
      profit_toman: 80_000,
    }),
  ],
  totals: period("", {
    purchases: 6, renewals: 3, gb_sold: 170, gb_free: 20, gb_billable: 150,
    cost_toman: 300_000, gross_sales_toman: 680_000, net_sales_toman: 680_000,
    profit_toman: 380_000,
  }),
});

function renderFinance(payload: unknown = finance()) {
  server.use(
    http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
    // The shell fetches the dashboard for its «شارژها» badge on every storefront route.
    http.get("*/api/portal/storefronts/1/dashboard", () => HttpResponse.json(dashboard(1))),
    http.get("*/api/portal/storefronts/1/finance", () => HttpResponse.json(payload)),
  );
  return renderWithProviders(
    <Routes>
      <Route path="/portal/storefront/:shopId" element={<StorefrontShell />}>
        <Route path="finance" element={<StorefrontFinancePage />} />
      </Route>
    </Routes>,
    "/portal/storefront/1/finance",
  );
}

function renderWithProviders(element: ReactElement, initialPath: string) {
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

// The same figure legitimately appears in a stat tile, in that tile's sub-line, and again in the
// monthly table — so every assertion is scoped to the tile it belongs to.
function tile(label: string) {
  const card = screen.getByText(label).closest(".MuiCard-root");
  return within(card as HTMLElement);
}

describe("storefront finance report", () => {
  it("opens on the newest month with activity and shows GB, cost, revenue and profit", async () => {
    renderFinance();

    // Newest month, not the (possibly empty) current calendar month.
    await screen.findByText("گیگ محاسبه‌شده");
    expect(tile("گیگ محاسبه‌شده").getByText("۱۰۰ گیگابایت")).toBeInTheDocument();
    expect(tile("هزینه در فاکتور شما").getByText("۲۰۰٬۰۰۰ تومان")).toBeInTheDocument();
    expect(tile("دریافتی از ربات").getByText("۵۰۰٬۰۰۰ تومان")).toBeInTheDocument();
    expect(tile("سود").getByText("۳۰۰٬۰۰۰ تومان")).toBeInTheDocument();
    // The cost tile states the arithmetic, so «هزینه = گیگ × نرخ» is checkable by eye.
    expect(tile("هزینه در فاکتور شما").getByText("۱۰۰ گیگابایت × ۲٬۰۰۰ تومان")).toBeInTheDocument();
    // The free test quota is called out rather than silently folded into the billable figure.
    expect(
      screen.getByText("از ۱۲۰ گیگابایت فروش، ۲۰ گیگابایت تست رایگان محاسبه نشد"),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "نمودار دریافتی و هزینه به تفکیک ماه" })).toBeInTheDocument();
  });

  it("switches to another month from the table without refetching", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/storefronts/1/dashboard", () => HttpResponse.json(dashboard(1))),
      http.get("*/api/portal/storefronts/1/finance", () => {
        calls += 1;
        return HttpResponse.json(finance());
      }),
    );
    renderWithProviders(
      <Routes>
        <Route path="/portal/storefront/:shopId" element={<StorefrontShell />}>
          <Route path="finance" element={<StorefrontFinancePage />} />
        </Route>
      </Routes>,
      "/portal/storefront/1/finance",
    );

    await screen.findByText("گیگ محاسبه‌شده");
    expect(tile("گیگ محاسبه‌شده").getByText("۱۰۰ گیگابایت")).toBeInTheDocument();
    await userEvent.click(screen.getByText("2026-06"));

    await waitFor(() =>
      expect(tile("گیگ محاسبه‌شده").getByText("۵۰ گیگابایت")).toBeInTheDocument());
    expect(tile("سود").getByText("۸۰٬۰۰۰ تومان")).toBeInTheDocument();
    expect(calls).toBe(1);   // every month arrives in one payload
  });

  it("labels a losing month as a loss rather than a negative profit", async () => {
    renderFinance({
      cost_per_gb_toman: 2000,
      excluded_below_gb: 1,
      months: [period("2026-05", {
        renewals: 1, gb_sold: 50, gb_billable: 50, cost_toman: 100_000, profit_toman: -100_000,
      })],
      totals: period("", {
        renewals: 1, gb_sold: 50, gb_billable: 50, cost_toman: 100_000, profit_toman: -100_000,
      }),
    });

    expect(await screen.findByText("زیان")).toBeInTheDocument();
    expect(tile("زیان").getByText("۱۰۰٬۰۰۰ تومان")).toBeInTheDocument();
    expect(screen.queryByText("سود")).not.toBeInTheDocument();   // never a negative "profit"
  });

  it("shows an empty state for a shop that has never sold", async () => {
    renderFinance({
      cost_per_gb_toman: 2000,
      excluded_below_gb: 1,
      months: [],
      totals: period(""),
    });

    expect(await screen.findByText("هنوز فروشی در این فروشگاه ثبت نشده است.")).toBeInTheDocument();
  });

  it("recovers from a failed load when the user retries", async () => {
    let fail = true;
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop(1)])),
      http.get("*/api/portal/storefronts/1/dashboard", () => HttpResponse.json(dashboard(1))),
      http.get("*/api/portal/storefronts/1/finance", () => {
        if (fail) {
          fail = false;
          return new HttpResponse(null, { status: 500 });
        }
        return HttpResponse.json(finance());
      }),
    );
    renderWithProviders(
      <Routes>
        <Route path="/portal/storefront/:shopId" element={<StorefrontShell />}>
          <Route path="finance" element={<StorefrontFinancePage />} />
        </Route>
      </Routes>,
      "/portal/storefront/1/finance",
    );

    await userEvent.click(await screen.findByRole("button", { name: "تلاش دوباره" }));
    await screen.findByText("گیگ محاسبه‌شده");
    expect(tile("گیگ محاسبه‌شده").getByText("۱۰۰ گیگابایت")).toBeInTheDocument();
  });
});
