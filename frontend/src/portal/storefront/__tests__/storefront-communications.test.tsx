import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../../../theme";
import { server } from "../../../test/setup";
import StorefrontCampaignsPage from "../StorefrontCampaignsPage";

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

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/portal/storefront/1/campaigns"]}>
          <Routes>
            <Route path="/portal/storefront/:shopId" element={<Outlet context={{ shop }} />}>
              <Route path="campaigns" element={<StorefrontCampaignsPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

function mockAudience(count = 12) {
  server.use(http.get("*/api/portal/storefronts/1/audience/preview", () =>
    HttpResponse.json({ segment: "all", count, over_cap: false, sample: [], config_version: 1 })));
}
function mockHistory(items: unknown[] = []) {
  server.use(http.get("*/api/portal/storefronts/1/broadcasts", () =>
    HttpResponse.json({ items, next_cursor: null, config_version: 1 })));
}

describe("storefront campaigns", () => {
  it("shows the audience count and the at-least-once note", async () => {
    mockAudience(12);
    mockHistory();
    renderPage();
    expect(await screen.findByText(/۱۲ گیرنده/)).toBeInTheDocument();
    expect(screen.getByText(/حداقل یک‌بار/)).toBeInTheDocument();
  });

  it("enqueues a broadcast with exactly one request on double-click", async () => {
    mockAudience(12);
    mockHistory();
    let posted = 0;
    server.use(http.post("*/api/portal/storefronts/1/broadcasts", async ({ request }) => {
      posted += 1;
      const body = (await request.json()) as { segment: string; text: string };
      expect(body.segment).toBe("all");
      expect(body.text).toBe("سلام دوستان");
      return HttpResponse.json(
        { result: { job_id: 42, status: "queued", total: 12 }, config_version: 1 },
        { status: 202 },
      );
    }));
    // status poll after send opens the progress dialog
    server.use(http.get("*/api/portal/storefronts/1/broadcasts/42", () =>
      HttpResponse.json({ job: {
        id: 42, kind: "broadcast", segment: "all", status: "queued", text: "سلام دوستان",
        total: 12, sent: 0, blocked: 0, failed: 0, pending: 12, created_at: null, canceled_at: null,
      } })));

    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/۱۲ گیرنده/);
    await user.type(screen.getByLabelText("متن پیام"), "سلام دوستان");
    const sendBtn = screen.getByRole("button", { name: "ارسال" });
    await user.click(sendBtn);
    await waitFor(() => expect(posted).toBe(1));
    expect(await screen.findByText(/در صف ارسال قرار گرفت/)).toBeInTheDocument();
  });

  it("blocks sending to an empty audience", async () => {
    mockAudience(0);
    mockHistory();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/۰ گیرنده/);
    await user.type(screen.getByLabelText("متن پیام"), "hello");
    expect(screen.getByRole("button", { name: "ارسال" })).toBeDisabled();
  });
});
