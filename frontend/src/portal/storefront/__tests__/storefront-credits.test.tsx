import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../../../theme";
import { server } from "../../../test/setup";
import StorefrontCreditsPage from "../StorefrontCreditsPage";

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

const code = {
  id: 7,
  code: "WELCOME",
  kind: "percent",
  percent_off: 20,
  amount_toman: null,
  max_bonus_toman: 50_000,
  min_topup_toman: 0,
  is_gift: false,
  max_uses: 100,
  per_customer_limit: 1,
  used_count: 3,
  enabled: true,
  archived: false,
  archived_at: null,
  starts_at: null,
  expires_at: null,
  created_at: "2026-07-10T08:00:00Z",
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/portal/storefront/1/credits"]}>
          <Routes>
            <Route path="/portal/storefront/:shopId" element={<Outlet context={{ shop }} />}>
              <Route path="credits" element={<StorefrontCreditsPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

function mockList(items: unknown[]) {
  server.use(http.get("*/api/portal/storefronts/1/credits", () =>
    HttpResponse.json({ items, next_cursor: null, config_version: 1 })));
}

describe("storefront credit codes", () => {
  it("lists codes with value + usage", async () => {
    mockList([code]);
    renderPage();
    expect(await screen.findByText("WELCOME")).toBeInTheDocument();
    expect(screen.getByText(/۲۰٪/)).toBeInTheDocument();
    expect(screen.getByText(/درصدی/)).toBeInTheDocument();
  });

  it("creates a code with a single request", async () => {
    mockList([]);
    let posted = 0;
    server.use(http.post("*/api/portal/storefronts/1/credits", async ({ request }) => {
      posted += 1;
      const body = (await request.json()) as { code: string; kind: string; percent_off: number };
      expect(body.code).toBe("SUMMER");
      expect(body.kind).toBe("percent");
      expect(body.percent_off).toBe(15);
      return HttpResponse.json(
        { result: { credit: { ...code, id: 8, code: "SUMMER", percent_off: 15 } }, config_version: 1 },
        { status: 201 },
      );
    }));

    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/هنوز کدی ثبت نشده/);
    await user.click(screen.getByRole("button", { name: "کد جدید" }));
    await user.type(screen.getByLabelText("کد"), "SUMMER");
    await user.type(screen.getByLabelText("درصد"), "15");
    await user.click(screen.getByRole("button", { name: "ذخیره" }));
    await waitFor(() => expect(posted).toBe(1));
  });

  it("archives a code", async () => {
    mockList([code]);
    let archived = 0;
    server.use(http.post("*/api/portal/storefronts/1/credits/7/archive", () => {
      archived += 1;
      return HttpResponse.json({ result: { credit: { ...code, archived: true, enabled: false } }, config_version: 1 });
    }));

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("WELCOME");
    await user.click(screen.getByRole("button", { name: "بایگانی" }));
    await waitFor(() => expect(archived).toBe(1));
  });

  it("locks economic fields after a redemption (used_count>0)", async () => {
    mockList([code]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("WELCOME");
    await user.click(screen.getByRole("button", { name: "ویرایش" }));
    // The used code shows the after-redemption lock notice and hides the value field.
    expect(await screen.findByText(/فقط وضعیت فعال\/غیرفعال و تاریخ انقضا/)).toBeInTheDocument();
    expect(screen.queryByLabelText("درصد")).not.toBeInTheDocument();
  });
});
