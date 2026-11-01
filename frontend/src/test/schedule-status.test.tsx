import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ScheduleStatus from "../components/ScheduleStatus";
import { makeTheme } from "../theme";
import { server } from "./setup";

const state = {
  status: "applied", desired: { invoice_day: 1, invoice_hour: 9 },
  active: { invoice_day: 1, invoice_hour: 9 },
  next_invoice_at: "2026-12-01T09:00:00+03:30", monthly_run: null,
};
function show(data: Record<string, unknown>, fail = false) {
  server.use(http.get("*/api/settings/schedule", () => fail
    ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(data)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ThemeProvider theme={makeTheme("light")}>
    <ScheduleStatus />
  </ThemeProvider></QueryClientProvider>);
}

describe("worker-reported schedule status", () => {
  it("shows the applied schedule and actual next fire in Tehran time", async () => {
    show(state);
    expect(await screen.findByText("زمان‌بندی ذخیره‌شده اعمال شده است.")).toBeVisible();
    expect(screen.getByText("2026-12-01 09:00")).toBeVisible();
    expect(screen.queryByText(/آخرین برنامهٔ فعال/)).not.toBeInTheDocument();
  });
  it("distinguishes saved day 1 from active day 4 while waiting", async () => {
    show({ ...state, status: "pending", active: { invoice_day: 4, invoice_hour: 9 } });
    expect(await screen.findByText(/در انتظار اعمال تغییرات/)).toBeVisible();
    expect(screen.getByText(/آخرین برنامهٔ فعال: روز ۴/)).toBeVisible();
    expect(screen.queryByText("زمان‌بندی ذخیره‌شده اعمال شده است.")).not.toBeInTheDocument();
  });
  it("does not claim a stale worker has applied settings", async () => {
    show({ ...state, status: "unavailable", next_invoice_at: null });
    expect(await screen.findByText(/وضعیت زمان‌بند به‌روز نیست/)).toBeVisible();
    expect(screen.queryByText("2026-12-01 09:00")).not.toBeInTheDocument();
  });
  it("makes incomplete billing visible", async () => {
    show({ ...state, monthly_run: { period: "2026-10", status: "retry", attempts: 1 } });
    expect(await screen.findByText(/ارسال کامل نشده/)).toBeVisible();
    expect(screen.getByText("2026-10")).toBeVisible();
  });
  it("offers retry after a status request fails", async () => {
    show({}, true);
    expect(await screen.findByText("دریافت وضعیت زمان‌بندی انجام نشد.")).toBeVisible();
    expect(screen.getByRole("button", { name: "تلاش دوباره" })).toBeEnabled();
  });
});
