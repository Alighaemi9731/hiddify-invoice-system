import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeTheme } from "../theme";
import { server } from "./setup";
import Settings from "../pages/Settings";

/**
 * Settings holds a numeric edit as TEXT while it is typed, so the payload it PUTs must be parsed
 * back to a number. Sending the string "2500" for `default_price_per_gb` would silently corrupt a
 * live pricing setting, so the conversion is pinned here — as is the rule that an emptied field
 * blocks the save instead of writing 0.
 */
const setting = (key: string, value: unknown) => ({
  key, value, is_secret: false, has_value: true,
});

function renderSettings(onPatch?: (items: Array<{ key: string; value: unknown }>) => void) {
  server.use(
    http.get("*/api/settings", () => HttpResponse.json([
      setting("default_price_per_gb", 2000),
      setting("free_under_gb", 1),
    ])),
    http.patch("*/api/settings", async ({ request }) => {
      const body = await request.json() as { items: Array<{ key: string; value: unknown }> };
      onPatch?.(body.items);
      return HttpResponse.json({ ok: true });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ThemeProvider theme={makeTheme("light")}><Settings /></ThemeProvider>
    </QueryClientProvider>,
  );
}

const price = () => screen.getByLabelText(/قیمت پیش‌فرض هر گیگابایت/) as HTMLInputElement;
const saveButton = () => screen.getByRole("button", { name: /ذخیره تغییرات/ });

/** Only the active section renders its fields; the pricing tab owns the price setting. */
async function openPricing(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("tab", { name: /قیمت‌گذاری/ }));
  await waitFor(() => expect(price().value).toBe("2000"));
}

describe("Settings numeric fields", () => {
  it("can be cleared and retyped — the old value must not come back mid-edit", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openPricing(user);

    await user.clear(price());
    expect(price().value).toBe("");        // used to snap straight back to 2000
    await user.type(price(), "2500");
    expect(price().value).toBe("2500");
  });

  it("saves a parsed number, never the staged text", async () => {
    const user = userEvent.setup();
    let sent: Array<{ key: string; value: unknown }> = [];
    renderSettings((items) => { sent = items; });
    await openPricing(user);

    await user.clear(price());
    await user.type(price(), "2500");
    await user.click(saveButton());

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({ key: "default_price_per_gb", value: 2500 });
  });

  it("refuses to save an emptied number rather than writing 0", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openPricing(user);

    await user.clear(price());
    expect(saveButton()).toBeDisabled();

    await user.type(price(), "3000");
    expect(saveButton()).toBeEnabled();
  });
});
