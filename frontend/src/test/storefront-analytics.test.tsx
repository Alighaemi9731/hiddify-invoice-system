import { ReactElement } from "react";
import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeTheme } from "../theme";
import { server } from "./setup";
import StorefrontAnalytics from "../pages/StorefrontAnalytics";

vi.mock("../components/EChart", () => ({
  default: ({ ariaLabel }: { ariaLabel?: string }) => <div role="img" aria-label={ariaLabel || "نمودار"} />,
}));

const window_ = (net: number, orders = 4) => ({
  gross_toman: net + 50_000,
  reversals_toman: 50_000,
  net_toman: net,
  orders,
  purchase_count: 3, purchase_toman: net - 200_000,
  renewal_count: 1, renewal_toman: 150_000,
  unknown_count: 0, unknown_toman: 0,
});

const shopRow = (id: number, name: string, net: number, extra: Partial<any> = {}) => ({
  shop_id: id, reseller_id: id * 10, reseller_name: name, panel_key: `p${id}`,
  bot_username: `shop${id}_bot`, enabled: true, status: "active", shop_closed: false,
  health_error_class: null, plans: 3, customers: 40, new_customers: 5,
  active_customers_30d: 22, services_active: 30, expiring_3d: 0,
  net_sales_toman: net, orders: 6, today_net_toman: 0,
  wallet_liability_toman: 100_000, pending_topups_count: 0, pending_topups_toman: 0,
  last_sale_at: new Date().toISOString(), created_at: "2026-02-01T00:00:00Z",
  ...extra,
});

const payload = {
  period: "2026-07", period_start: "2026-07-01", period_end: "2026-07-31",
  previous_period: "2026-06", generated_at: "2026-07-20T08:30:00Z",
  bots: {
    total: 4, enabled: 3, disabled: 1, active: 3, errored: 1, closed: 1, selling: 2,
    without_plans: 1, trial_enabled: 3, channel_locked: 1, panel_unhealthy: 0,
    new_in_period: 1, eligible_resellers: 8,
  },
  customers: {
    total: 120, new_today: 4, new_in_period: 25, active_7d: 45, active_30d: 80, banned: 2,
    buyers_in_period: 30, repeat_buyers_in_period: 9, wallet_liability_toman: 2_400_000,
    avg_order_toman: 185_000, arppu_toman: 320_000,
  },
  services: {
    total: 90, pending: 2, provisioned: 60, renewing: 3, disabled: 10, failed: 4, deleted: 11,
    active: 63, trials_active: 5, trials_in_period: 12, expiring_3d: 7, expiring_7d: 14,
    expired: 3, high_usage: 6, quota_gb: 1_250, used_gb: 800, autorenew_armed: 8,
  },
  topups: {
    pending_count: 3, pending_toman: 900_000, confirmed_count: 40, confirmed_toman: 9_000_000,
    rejected_count: 2,
    by_method: [
      { method: "card", count: 30, amount_toman: 7_000_000 },
      { method: "usdt", count: 10, amount_toman: 2_000_000 },
    ],
  },
  credits: { redemptions: 6, bonus_toman: 300_000, active_codes: 4 },
  operations: { pending: 1, in_progress: 2, done: 220, failed: 5, reversed: 3, failed_24h: 2 },
  trial: { trial_customers: 40, converted_customers: 10, rate: 0.25 },
  sales_today: window_(500_000, 3),
  sales_yesterday: window_(250_000, 2),
  sales_7d: window_(3_000_000, 18),
  sales_30d: window_(11_000_000, 60),
  sales_period: window_(9_600_000, 52),
  sales_previous_period: window_(8_000_000, 44),
  daily: Array.from({ length: 31 }, (_, i) => ({
    date: `2026-07-${String(i + 1).padStart(2, "0")}`, day: i + 1,
    net_toman: i === 0 ? 400_000 : 0, orders: i === 0 ? 2 : 0,
    new_customers: i === 0 ? 3 : 0, topups_toman: i === 0 ? 600_000 : 0,
  })),
  top_plans: [{ gb: 50, days: 30, orders: 20, amount_toman: 4_000_000 }],
  shops: [
    shopRow(1, "نمایندهٔ الف", 6_000_000),
    shopRow(2, "نمایندهٔ ب", 3_600_000, {
      status: "errored", plans: 0, pending_topups_count: 3, pending_topups_toman: 900_000,
      expiring_3d: 7, last_sale_at: null,
    }),
  ],
};

function renderPage(body: any = payload) {
  server.use(http.get("*/api/storefront-analytics", () => HttpResponse.json(body)));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const tree: ReactElement = (
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <StorefrontAnalytics />
      </QueryClientProvider>
    </ThemeProvider>
  );
  return render(tree);
}

describe("storefront analytics page", () => {
  it("leads with the live fleet KPIs and their period-over-period context", async () => {
    renderPage();

    // «ready to sell» is the honest headline: enabled AND healthy AND not temporarily closed.
    expect(await screen.findByText("ربات‌های در حال فروش")).toBeInTheDocument();
    expect(screen.getByText("۲")).toBeInTheDocument();
    expect(screen.getByText(/۴ ربات ثبت‌شده · ۱ خطادار/)).toBeInTheDocument();
    // today vs yesterday, and this period vs the previous one
    expect(screen.getByText(/۱۰۰٪ نسبت به دیروز/)).toBeInTheDocument();
    expect(screen.getByText(/۲۰٪ نسبت به دورهٔ قبل/)).toBeInTheDocument();
    expect(screen.getByText("۲۵٪")).toBeInTheDocument();               // trial → paid
    // the banner surfaces what needs the owner's attention right now
    expect(screen.getByText(/۱ ربات با توکن خراب/)).toBeInTheDocument();
    expect(screen.getByText(/۳ شارژ در انتظار تأیید فروشنده/)).toBeInTheDocument();
  });

  it("flags the shops that need attention with a concrete reason", async () => {
    renderPage();

    const card = (await screen.findByText("نیازمند رسیدگی")).closest(".MuiCard-root")!;
    expect(within(card as HTMLElement).getByText(/توکن ربات کار نمی‌کند/)).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText(/هیچ پلن فعالی ندارد/)).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText(/۷ سرویس تا ۳ روز دیگر منقضی/)).toBeInTheDocument();
    // a shop with sales today and no warnings must not be listed
    expect(within(card as HTMLElement).queryByText("نمایندهٔ الف")).not.toBeInTheDocument();
  });

  it("switches to the sales tab and shows the window comparison and best sellers", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("tab", { name: "فروش" }));

    expect(await screen.findByText("مقایسهٔ بازه‌ها")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "روند فروش روزانهٔ فروشگاه‌ها" })).toBeInTheDocument();
    expect(screen.getByText("۵۰ گیگابایت · ۳۰ روزه")).toBeInTheDocument();
    expect(screen.getByText("کارت‌به‌کارت")).toBeInTheDocument();
  });

  it("lists every shop in a searchable table", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("tab", { name: "فروشگاه‌ها" }));
    expect(await screen.findByText("نمایندهٔ الف")).toBeInTheDocument();
    expect(screen.getByText("نمایندهٔ ب")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("جست‌وجوی نماینده، ربات یا پنل"), "shop2");
    expect(await screen.findByText("نمایندهٔ ب")).toBeInTheDocument();
    expect(screen.queryByText("نمایندهٔ الف")).not.toBeInTheDocument();
  });

  it("stays readable when no shop has sold anything yet", async () => {
    renderPage({
      ...payload,
      sales_today: window_(0, 0), sales_yesterday: window_(0, 0),
      sales_period: { ...window_(0, 0), gross_toman: 0, reversals_toman: 0 },
      sales_previous_period: { ...window_(0, 0), gross_toman: 0, reversals_toman: 0 },
      daily: payload.daily.map((d) => ({ ...d, net_toman: 0, orders: 0 })),
      top_plans: [],
      shops: [],
    });

    expect(await screen.findByText("ربات‌های در حال فروش")).toBeInTheDocument();
    expect(screen.getByText(/دیروز: بدون فروش/)).toBeInTheDocument();
    expect(screen.getByText("در این دوره هیچ فروشگاهی فروش نداشته است.")).toBeInTheDocument();
  });
});
