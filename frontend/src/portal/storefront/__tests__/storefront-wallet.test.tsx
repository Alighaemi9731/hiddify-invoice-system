import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../../../theme";
import { fmtToman } from "../../../format";
import { server } from "../../../test/setup";
import CustomerDetailPage from "../CustomerDetailPage";

const shop = {
  id: 1,
  reseller: { id: 10, name: "فروشگاه آزمون" },
  panel: { id: 100, key: "panel-1" },
  bot_username: "test_shop_bot",
  enabled: true,
  status: "active",
  health_error_class: null,
  health_state_updated_at: null,
  shop_closed: false,
  role: "owner",
};

const customer = {
  id: 5,
  telegram_id: 111222333,
  name: "علی رضایی",
  username: "alirez",
  banned: false,
  free_trial_used: true,
  wallet_balance_toman: 50_000,
  net_ltv_toman: 120_000,
  last_seen_at: "2026-07-10T08:00:00Z",
  created_at: "2026-01-01T08:00:00Z",
  service_counts: { total: 0, active: 0, by_status: {} },
};

function mockDetail() {
  server.use(
    http.get("*/api/portal/storefronts/1/customers/5", () => HttpResponse.json(customer)),
    http.get("*/api/portal/storefronts/1/customers/5/orders", () =>
      HttpResponse.json({ items: [], next_cursor: null })),
    http.get("*/api/portal/storefronts/1/customers/5/ledger", () =>
      HttpResponse.json({ items: [], next_cursor: null })),
  );
}

function renderDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/portal/storefront/1/customers/5"]}>
          <Routes>
            <Route path="/portal/storefront/:shopId" element={<Outlet context={{ shop }} />}>
              <Route path="customers/:customerId" element={<CustomerDetailPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("storefront wallet adjustments", () => {
  it("shows requested vs applied distinctly when a debit is clamped at zero (overdraw)", async () => {
    mockDetail();
    let body: Record<string, unknown> | null = null;
    server.use(http.post("*/api/portal/storefronts/1/customers/5/wallet-adjustments", async ({ request }) => {
      body = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        result: {
          ledger_id: 42, requested_delta: -80_000, applied_delta: -50_000,
          old_balance: 50_000, new_balance: 0,
        },
        config_version: 1,
      });
    }));

    const user = userEvent.setup();
    renderDetail();
    expect(await screen.findByText("تنظیم دستی موجودی")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/مبلغ \(تومان\)/), "-80000");
    await user.type(screen.getByLabelText(/دلیل/), "کسر بیش از حد");
    await user.click(screen.getByRole("button", { name: "ثبت تغییر موجودی" }));

    await waitFor(() => expect(body).toEqual({ amount_toman_signed: -80_000, reason: "کسر بیش از حد" }));

    const deltas = await screen.findByText(/تغییر درخواستی/);
    // Requested (80,000) and applied (50,000) are shown as separate figures.
    expect(deltas).toHaveTextContent("تغییر درخواستی");
    expect(deltas).toHaveTextContent("تغییر اعمال‌شده");
    expect(deltas).toHaveTextContent(fmtToman(80_000));
    expect(deltas).toHaveTextContent(fmtToman(50_000));
    expect(screen.getByText(/موجودی نمی‌تواند منفی شود/)).toBeInTheDocument();
    // Balance transition old → new.
    expect(screen.getByText(/موجودی از/)).toHaveTextContent(
      `موجودی از ${fmtToman(50_000)} به ${fmtToman(0)} تغییر کرد.`);
  });

  it("issues a single adjustment for a double-click, reusing one Idempotency-Key", async () => {
    mockDetail();
    let requests = 0;
    const keys = new Set<string>();
    server.use(http.post("*/api/portal/storefronts/1/customers/5/wallet-adjustments", async ({ request }) => {
      requests += 1;
      keys.add(request.headers.get("Idempotency-Key") || "");
      await delay(60);
      return HttpResponse.json({
        result: {
          ledger_id: 43, requested_delta: 10_000, applied_delta: 10_000,
          old_balance: 50_000, new_balance: 60_000,
        },
        config_version: 1,
      });
    }));

    const user = userEvent.setup();
    renderDetail();
    await user.type(await screen.findByLabelText(/مبلغ \(تومان\)/), "10000");
    await user.type(screen.getByLabelText(/دلیل/), "پاداش وفاداری");
    await user.dblClick(screen.getByRole("button", { name: "ثبت تغییر موجودی" }));

    await waitFor(() => expect(requests).toBe(1));
    expect(keys.size).toBe(1);
    expect(await screen.findByText(/موجودی از/)).toHaveTextContent(
      `موجودی از ${fmtToman(50_000)} به ${fmtToman(60_000)} تغییر کرد.`);
  });

  it("gates the submit until a non-zero amount and a 3..255 reason are present", async () => {
    mockDetail();
    const user = userEvent.setup();
    renderDetail();
    const submit = await screen.findByRole("button", { name: "ثبت تغییر موجودی" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/دلیل/), "اصلاح دستی");
    expect(submit).toBeDisabled(); // no amount yet

    await user.type(screen.getByLabelText(/مبلغ \(تومان\)/), "0");
    expect(submit).toBeDisabled(); // a zero delta is rejected

    await user.clear(screen.getByLabelText(/مبلغ \(تومان\)/));
    await user.type(screen.getByLabelText(/مبلغ \(تومان\)/), "5000");
    expect(submit).toBeEnabled();
  });
});
