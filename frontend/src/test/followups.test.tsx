import { ReactElement } from "react";
import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeTheme } from "../theme";
import { server } from "./setup";
import Followups from "../pages/Followups";

vi.mock("../components/EChart", () => ({
  default: ({ ariaLabel }: { ariaLabel?: string }) =>
    <div role="img" aria-label={ariaLabel || "نمودار"} />,
}));

const row = (id: number, name: string, segment: string, extra: Partial<any> = {}) => ({
  reseller_id: id, reseller_name: name, admin_uuid: `uuid-${id}`,
  panel_id: 1, panel_key: "p1", segment, sub_resellers: 0, registered: true,
  value_at_risk_toman: 500_000 - id * 1000,
  mtd_services: 0, mtd_gb: 0, projected_gb: 0, avg_prev_gb: 40,
  last_sale_date: "2026-05-31", days_since_last_sale: 81, account_age_days: 400,
  outstanding_toman: 0, outstanding_count: 0, oldest_unpaid_period: null,
  last_touch_at: null, touch_count: 0, snoozed_until: null, muted: false, note: "",
  due: true, trend_gb: [10, 0, 40, 0, 0, 0],
  ...extra,
});

const summary = {
  counts: {
    suspended: 1, frozen: 0, debtor: 2, never_active: 4, onboarding: 0,
    churned: 3, dormant: 5, declining: 0, growing: 0, healthy: 20,
  },
  total: 35, due: 12, snoozed: 2, muted: 1, snooze_default_days: 15,
  generated_at: "2026-08-20T09:00:00Z",
};

const detail = {
  row: row(1, "نمایندهٔ الف", "churned", { note: "مشتری قدیمی", touch_count: 1 }),
  months: [
    { label: "2026-05", gb: 120, services: 4, amount_toman: 240_000 },
    { label: "2026-06", gb: 0, services: 0, amount_toman: 0 },
  ],
  followups: [
    {
      id: 7, reseller_id: 1, reseller_name: "نمایندهٔ الف", reseller_admin_uuid: "uuid-1",
      panel_key: "p1", segment: "dormant", note: "زنگ زدم، جواب نداد",
      snoozed_until: "2026-09-01", muted: false, actor: "owner",
      created_at: "2026-08-10T12:00:00Z",
    },
  ],
};

function renderPage(opts: { rows?: any[]; onFollowup?: (body: any) => void } = {}) {
  const rows = opts.rows ?? [
    row(1, "نمایندهٔ الف", "churned"),
    row(2, "نمایندهٔ ب", "never_active", { last_sale_date: null, days_since_last_sale: null }),
    row(3, "نمایندهٔ ج", "dormant", { outstanding_toman: 250_000, outstanding_count: 1 }),
  ];
  server.use(
    http.get("*/api/panels", () => HttpResponse.json([{ id: 1, key: "p1" }])),
    http.get("*/api/crm/summary", () => HttpResponse.json(summary)),
    http.get("*/api/crm/board", ({ request }) => {
      const params = new URL(request.url).searchParams;
      const seg = params.get("segment");
      const shown = seg ? rows.filter((r) => r.segment === seg) : rows;
      return HttpResponse.json(shown, { headers: { "X-Total-Count": String(shown.length) } });
    }),
    http.get("*/api/crm/reseller/1", () => HttpResponse.json(detail)),
    http.post("*/api/crm/reseller/:id/followup", async ({ request }) => {
      opts.onFollowup?.(await request.json());
      return HttpResponse.json({ updated: 1, snoozed_until: "2026-09-04", muted: false });
    }),
    http.post("*/api/crm/followups/bulk", async ({ request }) => {
      opts.onFollowup?.(await request.json());
      return HttpResponse.json({ updated: 2, snoozed_until: "2026-09-04", muted: false });
    }),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const tree: ReactElement = (
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <Followups />
      </QueryClientProvider>
    </ThemeProvider>
  );
  return render(tree);
}

describe("reseller follow-up board", () => {
  it("leads with the work queue and the churn buckets behind it", async () => {
    renderPage();

    // The tiles render their labels before the summary lands, so wait on the VALUE.
    const tileValue = async (label: string, value: string) => {
      const card = (await screen.findAllByText(label))[0].closest(".MuiCard-root")!;
      return within(card as HTMLElement).findByText(value);
    };

    expect(await tileValue("نیازمند پیگیری", "۱۲")).toBeInTheDocument();
    expect(screen.getByText(/از ۳۵ نمایندهٔ سطح‌یک/)).toBeInTheDocument();
    // dormant + churned is the "drifting away" signal the owner asked for
    expect(await tileValue("خوابیده و ریزش‌کرده", "۸")).toBeInTheDocument();
    expect(await tileValue("هرگز فعال نشده", "۴")).toBeInTheDocument();
    // suspended + frozen + debtor
    expect(await tileValue("مسدود و بدهکار", "۳")).toBeInTheDocument();
  });

  it("shows every reseller exactly once, in one segment", async () => {
    renderPage();

    expect(await screen.findByText("نمایندهٔ الف")).toBeInTheDocument();
    // One chip per row — no reseller shows up under two buckets. (The filter chips above
    // carry their count, so "ریزش‌کرده (۳)" is a different string from the row's chip.)
    expect(screen.getAllByText("ریزش‌کرده")).toHaveLength(1);
    // "هرگز فعال نشده" is also a KPI tile label, hence two matches — but only ONE row chip.
    const table = screen.getByRole("table");
    expect(within(table).getAllByText("هرگز فعال نشده")).toHaveLength(1);
    expect(within(table).getByText("هرگز")).toBeInTheDocument();   // never sold → no date
  });

  it("filters to a single segment from the chip row", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText("خوابیده (۵)"));
    expect(await screen.findByText("نمایندهٔ ج")).toBeInTheDocument();
    expect(screen.queryByText("نمایندهٔ الف")).not.toBeInTheDocument();
  });

  it("records a follow-up with a note and the owner's default snooze", async () => {
    const user = userEvent.setup();
    let sent: any = null;
    renderPage({ onFollowup: (b) => { sent = b; } });

    const rows = await screen.findAllByRole("button", { name: "پیگیری" });
    await user.click(rows[0]);
    expect(await screen.findByText(/این فرم هیچ پیامی نمی‌فرستد/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("یادداشت این پیگیری"), "زنگ زدم");
    await user.click(screen.getByRole("button", { name: "ثبت پیگیری" }));

    await screen.findByText(/پیگیری برای ۱ نماینده ثبت شد/);
    expect(sent).toMatchObject({ note: "زنگ زدم", snooze_days: 15, muted: false });
  });

  it("supports a bulk follow-up after a batch of manual DMs", async () => {
    const user = userEvent.setup();
    let sent: any = null;
    renderPage({ onFollowup: (b) => { sent = b; } });

    await user.click(await screen.findByRole("checkbox", { name: "انتخاب نمایندهٔ الف" }));
    await user.click(screen.getByRole("checkbox", { name: "انتخاب نمایندهٔ ب" }));
    expect(screen.getByText("۲ نماینده انتخاب شده")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "ثبت پیگیری گروهی" }));
    await user.click(screen.getByRole("button", { name: "ثبت پیگیری" }));

    await screen.findByText(/پیگیری برای ۲ نماینده ثبت شد/);
    expect(sent.reseller_ids).toEqual([1, 2]);
    // The pinned per-reseller note is single-target only — a bulk touch must not overwrite
    // three different resellers' notes with one string.
    expect(sent.pinned_note).toBeUndefined();
  });

  it("opens the reseller card with its history and past follow-ups", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "نمایندهٔ الف" }));
    const drawer = await screen.findByRole("presentation");
    expect(within(drawer).getByRole("img", { name: /نمودار حجم فروش ماهانهٔ/ })).toBeInTheDocument();
    expect(within(drawer).getByText("مشتری قدیمی")).toBeInTheDocument();
    expect(within(drawer).getByText("زنگ زدم، جواب نداد")).toBeInTheDocument();
  });

  it("says so plainly when nothing is left to chase", async () => {
    renderPage({ rows: [] });

    expect(await screen.findByText(
      "هیچ نماینده‌ای در این فیلتر نیاز به پیگیری ندارد — همه را رسیده‌اید."
    )).toBeInTheDocument();
  });
});
