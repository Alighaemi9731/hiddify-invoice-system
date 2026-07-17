import { ReactElement } from "react";
import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../../../theme";
import { server } from "../../../test/setup";
import CustomersPage from "../CustomersPage";
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

const listItem = {
  id: 5,
  telegram_id: 111222333,
  name: "علی رضایی",
  username: "alirez",
  banned: false,
  free_trial_used: true,
  wallet_balance_toman: 50_000,
  last_seen_at: "2026-07-10T08:00:00Z",
  created_at: "2026-01-01T08:00:00Z",
  has_service: true,
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
  service_counts: { total: 2, active: 1, by_status: { provisioned: 1, disabled: 1 } },
};

const order = {
  id: 9,
  customer_id: 5,
  label: "سرویس طلایی",
  status: "provisioned",
  is_trial: false,
  gb: 50,
  days: 30,
  price_toman: 200_000,
  created_at: "2026-07-01T08:00:00Z",
  last_renewed_at: null,
  live_refreshed_at: null,
  snapshot: {
    used_gb: 12.5,
    limit_gb: 50,
    start_date: "2026-07-01T00:00:00Z",
    enabled: true,
    last_online: "2026-07-10T09:00:00Z",
  },
  freshness: "stored",
};

const ledgerRow = {
  id: 1,
  kind: "purchase",
  amount_toman: -200_000,
  status: "confirmed",
  method: "wallet",
  order_id: 9,
  txid: null,
  chain: null,
  note: "خرید سرویس طلایی",
  created_at: "2026-07-01T08:00:00Z",
  decided_at: "2026-07-01T08:00:00Z",
};

function renderStorefront(path: string, element?: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const rendered = render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/portal/storefront/:shopId" element={element ?? <Outlet context={{ shop }} />}>
              <Route path="customers" element={<CustomersPage />} />
              <Route path="customers/:customerId" element={<CustomerDetailPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return { ...rendered, queryClient };
}

function mockDetail(overrides: {
  customer?: unknown;
  orders?: unknown[];
  ledger?: unknown[];
} = {}) {
  server.use(
    http.get("*/api/portal/storefronts/1/customers/5", () =>
      HttpResponse.json(overrides.customer ?? customer)),
    http.get("*/api/portal/storefronts/1/customers/5/orders", () =>
      HttpResponse.json({ items: overrides.orders ?? [order], next_cursor: null })),
    http.get("*/api/portal/storefronts/1/customers/5/ledger", () =>
      HttpResponse.json({ items: overrides.ledger ?? [ledgerRow], next_cursor: null })),
  );
}

describe("storefront customers list", () => {
  it("shows a loading skeleton then the empty state", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(http.get("*/api/portal/storefronts/1/customers", async () => {
      calls += 1;
      if (calls === 1) {
        await delay(40);
        return HttpResponse.json({ detail: "temporary" }, { status: 503 });
      }
      return HttpResponse.json({ items: [], next_cursor: null });
    }));

    const { container } = renderStorefront("/portal/storefront/1/customers");
    expect(container.querySelector(".MuiSkeleton-root")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /تلاش دوباره/ }));
    expect(await screen.findByText("مشتری‌ای با این فیلترها یافت نشد.")).toBeInTheDocument();
  });

  it("renders a non-leaking error state on server failure", async () => {
    server.use(http.get("*/api/portal/storefronts/1/customers", () =>
      HttpResponse.json({ detail: "boom" }, { status: 500 })));
    renderStorefront("/portal/storefront/1/customers");
    expect(await screen.findByText(/خطا در بارگذاری اطلاعات/)).toBeInTheDocument();
    expect(screen.queryByText("boom")).not.toBeInTheDocument();
  });

  it("deep-links a customer row to the detail page", async () => {
    server.use(http.get("*/api/portal/storefronts/1/customers", () =>
      HttpResponse.json({ items: [listItem], next_cursor: null })));
    mockDetail();

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers");
    await user.click(await screen.findByRole("button", { name: /باز کردن مشتری علی رضایی/ }));
    expect(await screen.findByText("هویت مشتری")).toBeInTheDocument();
    expect(await screen.findByText("وضعیت و دسترسی")).toBeInTheDocument();
  });

  it("sends a usable query but never a too-short name (no 422)", async () => {
    const seen: string[] = [];
    server.use(http.get("*/api/portal/storefronts/1/customers", ({ request }) => {
      seen.push(new URL(request.url).searchParams.get("q") || "");
      return HttpResponse.json({ items: [], next_cursor: null });
    }));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers");
    await screen.findByText("مشتری‌ای با این فیلترها یافت نشد.");
    const search = screen.getByLabelText(/جست‌وجو/);
    // An exact numeric telegram-id is a usable query and reaches the server.
    await user.type(search, "111222333");
    await waitFor(() => expect(seen[seen.length - 1]).toBe("111222333"));
    // A short (< 3 char) name is gated locally and never sent, so the server can't 422.
    await user.clear(search);
    await user.type(search, "عل");
    await waitFor(() => expect(seen[seen.length - 1]).toBe(""));
    expect(seen).not.toContain("عل");
  });

  it("sends the banned filter to the server", async () => {
    const seen: Array<string | null> = [];
    server.use(http.get("*/api/portal/storefronts/1/customers", ({ request }) => {
      seen.push(new URL(request.url).searchParams.get("banned"));
      return HttpResponse.json({ items: [], next_cursor: null });
    }));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers");
    await screen.findByText("مشتری‌ای با این فیلترها یافت نشد.");
    await user.click(screen.getByRole("combobox", { name: "وضعیت مسدودی" }));
    await user.click(await screen.findByRole("option", { name: "مسدود" }));
    await waitFor(() => expect(seen[seen.length - 1]).toBe("true"));
  });

  it("loads the next keyset page with the opaque cursor (no page numbers)", async () => {
    const cursors: Array<string | null> = [];
    server.use(http.get("*/api/portal/storefronts/1/customers", ({ request }) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      cursors.push(cursor);
      if (!cursor) {
        return HttpResponse.json({ items: [listItem], next_cursor: "CURSOR-2" });
      }
      return HttpResponse.json({
        items: [{ ...listItem, id: 6, telegram_id: 444555666, name: "زهرا محمدی" }],
        next_cursor: null,
      });
    }));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers");
    expect(await screen.findByText("علی رضایی")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "2" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "نمایش بیشتر" }));
    expect(await screen.findByText("زهرا محمدی")).toBeInTheDocument();
    expect(screen.getByText("علی رضایی")).toBeInTheDocument();
    expect(cursors).toEqual([null, "CURSOR-2"]);
  });
});

describe("storefront customer detail", () => {
  it("renders a 404 as a non-leaking not-found notice", async () => {
    server.use(http.get("*/api/portal/storefronts/1/customers/5", () =>
      HttpResponse.json({ detail: { code: "not_found" } }, { status: 404 })));
    renderStorefront("/portal/storefront/1/customers/5");
    expect(await screen.findByText(/این مشتری پیدا نشد/)).toBeInTheDocument();
  });

  it("shows read-only wallet figures with no money-adjustment controls", async () => {
    mockDetail();
    renderStorefront("/portal/storefront/1/customers/5");
    expect(await screen.findByText("کیف پول (فقط نمایش)")).toBeInTheDocument();
    expect(screen.getByText("تغییر موجودی کیف پول از این صفحه امکان‌پذیر نیست.")).toBeInTheDocument();
    // No top-up / credit / debit / balance-adjust actions on this page.
    expect(screen.queryByRole("button", { name: /شارژ|افزودن موجودی|کسر|اعتبار|واریز|موجودی/ })).toBeNull();
  });

  it("issues a single renew request for a double-click", async () => {
    mockDetail();
    let requests = 0;
    server.use(http.post("*/api/portal/storefronts/1/orders/9/renew", async () => {
      requests += 1;
      await delay(60);
      return HttpResponse.json({ result: { order: { id: 9, renewed: true, gb: 50, days: 30 } }, config_version: 1 });
    }));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers/5");
    await user.click(await screen.findByRole("button", { name: "تمدید رایگان" }));
    await user.dblClick(await screen.findByRole("button", { name: "تأیید" }));

    await waitFor(() => expect(requests).toBe(1));
    expect(await screen.findByText(/سرویس تمدید شد/)).toBeInTheDocument();
  });

  it("requires typing DELETE and a reason before deleting a service", async () => {
    mockDetail();
    let body: Record<string, unknown> | null = null;
    server.use(http.delete("*/api/portal/storefronts/1/orders/9", async ({ request }) => {
      body = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ result: { order: { id: 9, status: "deleted" } }, config_version: 1 });
    }));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers/5");
    await user.click(await screen.findByRole("button", { name: "حذف سرویس" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "تأیید" });
    expect(confirm).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/عبارت DELETE/), "DELETE");
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/دلیل/), "درخواست مشتری");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(body).toEqual({ confirm: "DELETE", reason: "درخواست مشتری" }));
  });

  it("requires a reason to ban and sends it", async () => {
    mockDetail();
    let body: Record<string, unknown> | null = null;
    server.use(http.patch("*/api/portal/storefronts/1/customers/5/status", async ({ request }) => {
      body = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ result: { customer: { id: 5, banned: true } }, config_version: 1 });
    }));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers/5");
    await user.click(await screen.findByRole("button", { name: "مسدودسازی مشتری" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "تأیید" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/دلیل/), "تخلف مکرر");
    await user.click(confirm);

    await waitFor(() => expect(body).toEqual({ banned: true, reason: "تخلف مکرر" }));
    expect(await screen.findByText("مشتری مسدود شد.")).toBeInTheDocument();
  });

  it("treats a 429 live refresh as a soft retry, surfacing Retry-After", async () => {
    mockDetail();
    server.use(http.post("*/api/portal/storefronts/1/orders/9/refresh", () =>
      HttpResponse.json({ detail: { code: "rate_limited" } }, {
        status: 429,
        headers: { "Retry-After": "12" },
      })));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers/5");
    await user.click(await screen.findByRole("button", { name: "به‌روزرسانی وضعیت زنده" }));
    expect(await screen.findByText(/چند لحظه دیگر تلاش کنید \(۱۲ ثانیه\)/)).toBeInTheDocument();
  });

  it("shows the live status distinctly from the stored snapshot on refresh", async () => {
    mockDetail();
    server.use(http.post("*/api/portal/storefronts/1/orders/9/refresh", () =>
      HttpResponse.json({
        status: { ok: true, used_gb: 20, limit_gb: 50, remaining_days: 18 },
        freshness: "live",
      })));

    const user = userEvent.setup();
    renderStorefront("/portal/storefront/1/customers/5");
    // stored snapshot is present up front
    expect(await screen.findByText(/آخرین وضعیت ذخیره‌شده/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "به‌روزرسانی وضعیت زنده" }));
    expect(await screen.findByText(/وضعیت زنده \(زنده\)/)).toBeInTheDocument();
    expect(screen.getByText(/۱۸ روز باقی‌مانده/)).toBeInTheDocument();
  });
});
