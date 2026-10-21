import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import Invoices from "../pages/Invoices";
import { downloadCsv } from "../csv";
import { makeTheme } from "../theme";
import { server } from "./setup";

vi.mock("../csv", () => ({ downloadCsv: vi.fn() }));

describe("invoice month ordering", () => {
  it("requests oldest months first, keeps reseller scope in CSV, and allows manual sorting", async () => {
    const requests: URLSearchParams[] = [];
    const invoice = { id: 1, number: "10000001", reseller_id: 42, reseller_name: "Ali", panel_key: "p",
      period_label: "2026-01", amount_toman: 1000, usage_gb: 1, status: "paid" };
    server.use(
      http.get("*/api/invoices", ({ request }) => {
        requests.push(new URL(request.url).searchParams);
        return HttpResponse.json([invoice], { headers: { "X-Total-Count": "1", "X-Total-Amount-Toman": "1000" } });
      }),
      http.get("*/api/resellers", () => HttpResponse.json([{ id: 42, name: "Ali", panel_key: "p" }])),
      http.get("*/api/rates", () => HttpResponse.json({ effective: 100000 })),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<ThemeProvider theme={makeTheme("light")}><QueryClientProvider client={client}>
      <Invoices />
    </QueryClientProvider></ThemeProvider>);
    const user = userEvent.setup();
    expect(await screen.findByText("10000001")).toBeInTheDocument();
    expect(requests[0].get("sort")).toBe("period_label");
    expect(requests[0].get("order")).toBe("asc");
    await user.type(screen.getByPlaceholderText("فاکتورهای یک نماینده (همهٔ ماه‌ها)"), "Ali");
    await user.click(await screen.findByRole("option", { name: "Ali — p" }));
    await waitFor(() => expect(requests[requests.length - 1]?.get("reseller_id")).toBe("42"));
    expect(requests[requests.length - 1]?.has("period")).toBe(false);
    expect(requests[requests.length - 1]?.get("sort")).toBe("period_label");
    expect(requests[requests.length - 1]?.get("order")).toBe("asc");
    await user.click(screen.getByText("خروجی CSV"));
    await waitFor(() => expect(downloadCsv).toHaveBeenCalledWith("invoices-r42.csv", expect.any(Array), expect.any(Array)));
    const exported = requests.find((r) => r.get("limit") === "2000")!;
    expect(exported.get("reseller_id")).toBe("42");
    expect(exported.has("period")).toBe(false);
    expect(exported.get("sort")).toBe("period_label");
    expect(exported.get("order")).toBe("asc");
    await user.click(screen.getByText("مبلغ (تومان)"));
    await waitFor(() => expect(requests[requests.length - 1]?.get("sort")).toBe("amount_toman"));
    expect(requests[requests.length - 1]?.get("order")).toBe("asc");
    await user.click(screen.getByText("مبلغ (تومان)"));
    await waitFor(() => expect(requests[requests.length - 1]?.get("order")).toBe("desc"));
    client.clear();
  });
});
