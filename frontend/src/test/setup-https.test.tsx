import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import Setup from "../pages/Setup";
import { getSetupStatus, prepareSetupDomain } from "../api/client";

vi.mock("../api/client", () => ({
  getSetupStatus: vi.fn(), prepareSetupDomain: vi.fn(), doSetup: vi.fn(),
}));
afterEach(() => vi.unstubAllGlobals());

describe("HTTPS first-run bootstrap", () => {
  it("collects no owner password over public HTTP and moves to HTTPS before account setup", async () => {
    vi.stubGlobal("location", { protocol: "http:", hostname: "203.0.113.9", href: "" });
    vi.mocked(getSetupStatus).mockResolvedValue({ setup_done: false, domain: "", https_enabled: false, token_required: true });
    vi.mocked(prepareSetupDomain).mockResolvedValue({ ok: true, url: "https://panel.example.com" });
    const user = userEvent.setup();
    render(<Setup onDone={vi.fn()} />);
    await screen.findByLabelText("توکن فعال‌سازی HTTPS");
    expect(screen.queryByLabelText("نام کاربری")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("رمز عبور")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("دامنه"), "panel.example.com");
    await user.type(screen.getByLabelText("توکن فعال‌سازی HTTPS"), "install-token");
    await user.click(screen.getByRole("button", { name: "فعال‌سازی HTTPS و ادامه" }));
    await waitFor(() => expect(prepareSetupDomain).toHaveBeenCalledWith({ domain: "panel.example.com", token: "install-token", acme_email: undefined }));
    expect(await screen.findByText("اتصال امن آماده می‌شود")).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://panel.example.com");
  });

  it("offers owner creation on HTTPS", async () => {
    vi.stubGlobal("location", { protocol: "https:", hostname: "panel.example.com", href: "" });
    vi.mocked(getSetupStatus).mockResolvedValue({ setup_done: false, domain: "panel.example.com", https_enabled: true, token_required: true });
    render(<Setup onDone={vi.fn()} />);
    expect(await screen.findByLabelText("نام کاربری")).toBeInTheDocument();
    expect(screen.getByLabelText("رمز عبور")).toBeInTheDocument();
  });
});
