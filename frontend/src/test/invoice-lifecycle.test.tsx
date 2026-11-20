import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import Invoices from "../pages/Invoices";
import { makeTheme } from "../theme";
import { server } from "./setup";

it("shows the original compact table and filters undelivered invoices as drafts", async () => {
  const requests: URLSearchParams[] = [];
  server.use(
    http.get("*/api/invoices", ({ request }) => {
      requests.push(new URL(request.url).searchParams);
      return HttpResponse.json([
        { id: 1, number: "10000001", reseller_name: "Ali", panel_key: "p", period_label: "2026-11",
          amount_toman: 1000, usage_gb: 1, status: "sent", display_status: "draft", telegram_delivery_status: "unmatched" },
        { id: 2, number: "10000002", reseller_name: "Sara", panel_key: "p", period_label: "2026-11",
          amount_toman: 2000, usage_gb: 2, status: "enforced", display_status: "financial_blocked" },
        { id: 3, number: "10000003", reseller_name: "Reza", panel_key: "p", period_label: "2026-11",
          amount_toman: 3000, usage_gb: 3, status: "sent", display_status: "registration_blocked" },
      ], { headers: { "X-Total-Count": "3" } });
    }),
    http.get("*/api/ops/rates", () => HttpResponse.json({ effective: 100000 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<ThemeProvider theme={makeTheme("light")}><QueryClientProvider client={client}><Invoices /></QueryClientProvider></ThemeProvider>);
  expect(await screen.findByText("10000001")).toBeInTheDocument();
  expect(screen.getByText("پیش‌نویس")).toBeInTheDocument();
  expect(screen.getByText("مسدود مالی")).toBeInTheDocument();
  expect(screen.getByText("مسدود به‌دلیل عدم ثبت ربات")).toBeInTheDocument();
  expect(screen.queryByText("ارسال‌نشده؛ ثبت‌نام ربات ناقص")).not.toBeInTheDocument();
  expect(screen.queryByText("صدور قطعی دوره")).not.toBeInTheDocument();
  expect(screen.queryByText("ارسال تلگرام", { selector: "th" })).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByText("همهٔ وضعیت‌ها"));
  await user.click(screen.getByRole("option", { name: "پیش‌نویس" }));
  await waitFor(() => expect(requests[requests.length - 1]?.get("display_status")).toBe("draft"));
  expect(requests[requests.length - 1]?.has("delivery_status")).toBe(false);
  client.clear();
});
