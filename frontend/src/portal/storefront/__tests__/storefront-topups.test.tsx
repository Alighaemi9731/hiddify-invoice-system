import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../../../theme";
import { fmtToman } from "../../../format";
import { server } from "../../../test/setup";
import TopupsPage from "../TopupsPage";

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

const pendingTopup = {
  id: 9,
  customer: { id: 5, telegram_id: 111222333, name: "علی رضایی", username: "alirez" },
  amount_toman: 100_000,
  requested_amount_toman: 100_000,
  status: "pending",
  method: "card",
  chain: null,
  txid: null,
  has_proof: true,
  created_at: "2026-07-10T08:00:00Z",
  decided_at: null,
};

function renderTopups() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const rendered = render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/portal/storefront/1/topups"]}>
          <Routes>
            <Route path="/portal/storefront/:shopId" element={<Outlet context={{ shop }} />}>
              <Route path="topups" element={<TopupsPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return { ...rendered, queryClient };
}

// The queue badge reads the dashboard's pending count; every render must have it mocked.
function mockDashboard(pending = 3) {
  server.use(http.get("*/api/portal/storefronts/1/dashboard", () =>
    HttpResponse.json({ pending_topups: { count: pending, amount_toman: 0 } })));
}

function mockList(items: unknown[]) {
  server.use(http.get("*/api/portal/storefronts/1/topups", () =>
    HttpResponse.json({ items, next_cursor: null, total_hint: null })));
}

describe("storefront top-ups operations center", () => {
  it("scopes the queue to pending and forwards the method filter", async () => {
    const seen: Array<{ status: string | null; method: string | null }> = [];
    server.use(http.get("*/api/portal/storefronts/1/topups", ({ request }) => {
      const url = new URL(request.url);
      seen.push({ status: url.searchParams.get("status"), method: url.searchParams.get("method") });
      return HttpResponse.json({ items: [], next_cursor: null, total_hint: null });
    }));
    mockDashboard();

    const user = userEvent.setup();
    renderTopups();
    expect(await screen.findByText("شارژ در انتظاری برای بررسی وجود ندارد.")).toBeInTheDocument();
    expect(seen[seen.length - 1].status).toBe("pending");

    await user.click(screen.getByRole("combobox", { name: "روش" }));
    await user.click(await screen.findByRole("option", { name: "USDT" }));
    await waitFor(() => expect(seen[seen.length - 1].method).toBe("usdt"));
    expect(seen[seen.length - 1].status).toBe("pending"); // still the queue
  });

  it("issues a single confirm for a double-click, reusing one Idempotency-Key", async () => {
    mockDashboard();
    mockList([pendingTopup]);
    let requests = 0;
    const keys = new Set<string>();
    server.use(http.post("*/api/portal/storefronts/1/topups/9/decision", async ({ request }) => {
      requests += 1;
      keys.add(request.headers.get("Idempotency-Key") || "");
      await delay(60);
      return HttpResponse.json({
        result: {
          txn_id: 9, decision: "confirm", changed: true, already_decided: false,
          credited: 100_000, requested: 100_000, status: "confirmed",
        },
        config_version: 1,
      });
    }));

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("button", { name: "تأیید" }));
    const dialog = await screen.findByRole("dialog");
    await user.dblClick(within(dialog).getByRole("button", { name: "تأیید" }));

    await waitFor(() => expect(requests).toBe(1));
    expect(keys.size).toBe(1);
    expect(await screen.findByText(/به کیف پول افزوده شد/)).toBeInTheDocument();
  });

  it("heals an already-decided confirm as a success message", async () => {
    mockDashboard();
    mockList([pendingTopup]);
    server.use(http.post("*/api/portal/storefronts/1/topups/9/decision", () =>
      HttpResponse.json({
        result: {
          txn_id: 9, decision: "confirm", changed: false, already_decided: true,
          credited: 100_000, requested: 100_000, status: "confirmed",
        },
        config_version: 1,
      })));

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("button", { name: "تأیید" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "تأیید" }));
    expect(await screen.findByText("این شارژ پیش‌تر تعیین‌تکلیف شده بود.")).toBeInTheDocument();
  });

  it("disables the reject submit until the reason is 3..255 chars, then sends it", async () => {
    mockDashboard();
    mockList([pendingTopup]);
    let body: Record<string, unknown> | null = null;
    server.use(http.post("*/api/portal/storefronts/1/topups/9/decision", async ({ request }) => {
      body = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        result: {
          txn_id: 9, decision: "reject", changed: true, already_decided: false,
          credited: null, requested: 100_000, status: "rejected",
        },
        config_version: 1,
      });
    }));

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("button", { name: "رد" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "تأیید" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/دلیل/), "اس"); // 2 chars
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/دلیل/), "پم"); // now 4 chars
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(body).toEqual({ decision: "reject", reason: "اسپم" }));
    expect(await screen.findByText("شارژ رد شد.")).toBeInTheDocument();
  });

  it("runs a bounded bulk review with a single key and per-item results, healing already-decided", async () => {
    mockDashboard();
    mockList([
      { ...pendingTopup, id: 9 },
      { ...pendingTopup, id: 10 },
      { ...pendingTopup, id: 11 },
    ]);
    let bulkRequests = 0;
    const keys = new Set<string>();
    server.use(http.post("*/api/portal/storefronts/1/topups/bulk-decisions", async ({ request }) => {
      bulkRequests += 1;
      keys.add(request.headers.get("Idempotency-Key") || "");
      const payload = await request.json() as { items: Array<{ txn_id: number }> };
      expect(payload.items).toHaveLength(3);
      await delay(60);
      return HttpResponse.json({
        result: {
          results: [
            { txn_id: 9, result: "changed" },
            { txn_id: 10, result: "already_decided" },
            { txn_id: 11, result: "failed" },
          ],
          counts: { changed: 1, already_decided: 1, not_found: 0, failed: 1 },
        },
        config_version: 1,
      });
    }));

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("button", { name: "انتخاب موارد این صفحه" }));
    await user.click(await screen.findByRole("button", { name: "تأیید گروهی" }));
    const dialog = await screen.findByRole("dialog");
    await user.dblClick(within(dialog).getByRole("button", { name: "اعمال" }));

    expect(await screen.findByText("نتیجهٔ بررسی گروهی")).toBeInTheDocument();
    expect(bulkRequests).toBe(1);
    expect(keys.size).toBe(1);
    // changed (1) + already_decided (1) are healed together as success.
    expect(screen.getByText(/۲ مورد با موفقیت اعمال شد/)).toBeInTheDocument();
    expect(screen.getByText("اعمال شد")).toBeInTheDocument();
    expect(screen.getByText("قبلاً اعمال شده")).toBeInTheDocument();
    expect(screen.getByText("ناموفق")).toBeInTheDocument();
  });

  it("surfaces a 409 through the conflict dialog", async () => {
    mockDashboard();
    mockList([pendingTopup]);
    server.use(http.post("*/api/portal/storefronts/1/topups/9/decision", () =>
      HttpResponse.json({ detail: { code: "idempotency_conflict", message: "conflict" } }, { status: 409 })));

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("button", { name: "تأیید" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "تأیید" }));
    expect(await screen.findByText("اطلاعات فروشگاه تغییر کرده است")).toBeInTheDocument();
  });

  it("shows a not-found state when the proof image is missing (404)", async () => {
    mockDashboard();
    mockList([pendingTopup]);
    server.use(http.get("*/api/portal/storefronts/1/topups/9", () =>
      HttpResponse.json({ ...pendingTopup, note: null, credit_code_id: null })));
    server.use(http.get("*/api/portal/storefronts/1/topups/9/proof", () =>
      HttpResponse.json({ detail: { code: "not_found" } }, { status: 404 })));

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("button", { name: "مشاهدهٔ جزئیات" }));
    expect(await screen.findByText("رسید در دسترس نیست.")).toBeInTheDocument();
  });

  it("shows the credited amount distinctly from the requested when a confirm corrected it", async () => {
    mockDashboard();
    mockList([{
      ...pendingTopup, id: 12, status: "confirmed",
      requested_amount_toman: 100_000, amount_toman: 80_000, has_proof: false,
      decided_at: "2026-07-11T08:00:00Z",
    }]);

    const user = userEvent.setup();
    renderTopups();
    await user.click(await screen.findByRole("tab", { name: /تاریخچه/ }));
    expect(await screen.findByText(`اعتبارشده: ${fmtToman(80_000)}`)).toBeInTheDocument();
    expect(screen.getByText(`درخواست: ${fmtToman(100_000)}`)).toBeInTheDocument();
  });
});
