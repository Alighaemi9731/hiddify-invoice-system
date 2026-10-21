import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import Broadcast from "../pages/Broadcast";
import { recipientCsv } from "../pages/Broadcast/export";
import { makeTheme } from "../theme";
import { server } from "./setup";

const account = { reseller_id: 21, name: "نماینده آزمایشی", panel: "p1", periods: ["2026-07"],
  debt_toman: 450000, oldest_debt_days: 52, access_state: "enforced" };
const preview = { matched: 1, matched_accounts: 1, total: 1, unregistered: 0, duplicate_chats: 0,
  debt_toman: 450000, preview_token: "snapshot-1", recipients: [{ ...account, status: "pending", accounts: [account] }], skipped: [] };

function renderPage(onPreview = vi.fn(), onSend = vi.fn()) {
  server.use(
    http.get("*/api/panels", () => HttpResponse.json([{ id: 1, key: "p1" }, { id: 2, key: "p2" }])),
    http.get("*/api/ops/broadcast/options", () => HttpResponse.json({ periods: ["2026-08", "2026-07"] })),
    http.post("*/api/ops/broadcast/preview", async ({ request }) => { onPreview(await request.json()); return HttpResponse.json(preview); }),
    http.post("*/api/ops/broadcast", async ({ request }) => { onSend(await request.json()); return HttpResponse.json({ total: 1, unregistered: 0 }); }),
    http.get("*/api/ops/broadcast/status", () => HttpResponse.json({ running: false, finished_at: "done", sent: 1, blocked: 0, failed: 0, total: 1 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<ThemeProvider theme={makeTheme("light")}><QueryClientProvider client={client}><Broadcast /></QueryClientProvider></ThemeProvider>);
}

async function select(label: string, option: string) {
  await userEvent.click(screen.getByLabelText(label));
  await userEvent.click(within(await screen.findByRole("listbox")).getByRole("option", { name: option }));
}

describe("advanced broadcast", () => {
  it("combines invoice month, panels, debt and access, then confirms the preview snapshot", async () => {
    const onPreview = vi.fn(), onSend = vi.fn();
    renderPage(onPreview, onSend);
    await screen.findByLabelText("از ماه فاکتور");
    await select("از ماه فاکتور", "2026-07");
    await select("تا ماه فاکتور", "2026-07");
    await select("وضعیت دسترسی", "مسدود");
    await userEvent.click(screen.getByLabelText("پنل‌ها"));
    await userEvent.click(within(await screen.findByRole("listbox")).getByRole("option", { name: "p1" }));
    await userEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: "p2" }));
    await userEvent.keyboard("{Escape}");
    await userEvent.type(screen.getByLabelText("حداقل بدهی (تومان)"), "100000");
    await userEvent.click(screen.getByLabelText("حذف کسانی که رسید منتظر تأیید دارند"));
    await userEvent.type(screen.getByLabelText("متن پیام"), "یادآوری پرداخت");
    expect(screen.getByRole("button", { name: "بررسی نهایی و ارسال" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "پیش‌نمایشِ گیرندگان" }));
    await screen.findByText("نماینده آزمایشی");
    expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ filters: expect.objectContaining({
      panel_ids: [1, 2], period_from: "2026-07", period_to: "2026-07", access_state: "enforced", min_debt: 100000, exclude_pending: true,
    }) }));
    await userEvent.click(screen.getByRole("button", { name: "بررسی نهایی و ارسال" }));
    const dialog = await screen.findByRole("dialog");
    expect(onSend).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "تأیید و ارسال" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ preview_token: "snapshot-1", text: "یادآوری پرداخت" })));
  });

  it("invalidates the preview after a filter change and supports individual exclusions", async () => {
    const onPreview = vi.fn();
    renderPage(onPreview);
    await userEvent.type(screen.getByLabelText("متن پیام"), "سلام");
    await userEvent.click(screen.getByRole("button", { name: "پیش‌نمایشِ گیرندگان" }));
    await screen.findByText("نماینده آزمایشی");
    await userEvent.click(screen.getByRole("button", { name: "کنار گذاشتن" }));
    expect(screen.getByRole("button", { name: "بررسی نهایی و ارسال" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "پیش‌نمایشِ گیرندگان" }));
    await waitFor(() => expect(onPreview).toHaveBeenLastCalledWith(expect.objectContaining({ filters: { excluded_reseller_ids: [21] } })));
  });

  it("discards an in-flight preview when filters change before its response arrives", async () => {
    renderPage();
    let finish!: () => void;
    const pending = new Promise<void>(resolve => { finish = resolve; });
    server.use(http.post("*/api/ops/broadcast/preview", async () => { await pending; return HttpResponse.json(preview); }));
    await userEvent.type(screen.getByLabelText("متن پیام"), "سلام");
    await userEvent.click(screen.getByRole("button", { name: "پیش‌نمایشِ گیرندگان" }));
    await userEvent.type(screen.getByLabelText("نام یا شناسه نماینده"), "جدید");
    finish();
    await waitFor(() => expect(screen.getByRole("button", { name: "پیش‌نمایشِ گیرندگان" })).toBeEnabled());
    expect(screen.queryByText("نماینده آزمایشی")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "بررسی نهایی و ارسال" })).toBeDisabled();
  });

  it("exports every account and neutralizes reseller spreadsheet formulas", () => {
    const csv = recipientCsv([{ ...account, name: '=HYPERLINK("evil")' }, { ...account, reseller_id: 22 }]);
    expect(csv).toContain("\"'=HYPERLINK");
    expect(csv).toContain('"22"');
    expect(csv).toContain("2026-07");
  });
});
