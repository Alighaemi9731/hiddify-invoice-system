import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InvoiceAccess, InvoiceDeadlines, InvoiceDelivery } from "../components/InvoiceLifecycle";
import { INVOICE_STATUS_FA } from "../format";

describe("financial state and Telegram delivery", () => {
  it("describes an issued but unregistered invoice without claiming it was delivered", () => {
    render(<><span>{INVOICE_STATUS_FA.sent}</span><InvoiceDelivery invoice={{ status: "sent", telegram_delivery_status: "unmatched" }} /></>);
    expect(screen.getByText("صادرشده؛ پرداخت‌نشده")).toBeInTheDocument();
    expect(screen.getByText("ارسال‌نشده؛ ثبت‌نام ربات ناقص")).toBeInTheDocument();
    expect(screen.queryByText(/اولین ارسال/)).not.toBeInTheDocument();
  });
  it("keeps the first-delivery fact when the last resend fails", () => {
    render(<InvoiceDelivery invoice={{ status: "overdue", sent_at: "2026-11-17T12:00:00Z", telegram_delivery_status: "failed" }} />);
    expect(screen.getByText("ارسال ناموفق؛ تلاش مجدد لازم است")).toBeInTheDocument();
    expect(screen.getByText(/اولین ارسال:/)).toBeInTheDocument();
  });
  it("distinguishes registration suspension from financial deadlines", () => {
    render(<><InvoiceAccess invoice={{ reseller_access_state: "enforced", registration_blocked: true }} />
      <InvoiceDeadlines invoice={{ due_on: "2026-11-27", enforcement_on: "2026-12-17" }} /></>);
    expect(screen.getByText("سرویس تعلیق‌شده")).toBeInTheDocument();
    expect(screen.getByText("ثبت‌نام ناقص")).toBeInTheDocument();
    expect(screen.getByText(/سررسید:/)).toBeInTheDocument();
    expect(screen.getByText(/تعلیق مالی:/)).toBeInTheDocument();
  });
});
