import { ReactElement } from "react";
import { ThemeProvider } from "@mui/material";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeTheme } from "../../../theme";
import { server, setMatchMedia } from "../../../test/setup";
import StorefrontManagersPage from "../StorefrontManagersPage";
import StorefrontPlansPage from "../StorefrontPlansPage";
import StorefrontPreviewPage from "../StorefrontPreviewPage";
import StorefrontSettingsPage from "../StorefrontSettingsPage";
import StorefrontShell from "../StorefrontShell";
import { storefrontQueryKeys } from "../api";

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
  cost_per_gb_toman: 2000,
  role: "owner",
};

const plans = [
  { id: 1, gb: 10, days: 30, price_toman: 100_000, enabled: true, sort_order: 0 },
  { id: 2, gb: 20, days: 60, price_toman: 180_000, enabled: true, sort_order: 1 },
];

const settings = {
  payment: {
    pay_card_enabled: true,
    pay_usdt_enabled: false,
    pay_ton_enabled: false,
    card_number: null,
    card_holder: null,
    usdt_address: null,
    ton_address: null,
  },
  trial: {
    free_trial_enabled: true, free_trial_gb: 1, free_trial_days: 1,
    reset: {
      period: "2026-08", last_reset_period: null, available: true, reason: null,
      // A permissive ceiling on purpose: these tests are about draft/CAS behaviour, not the
      // owner's trial budget, and would otherwise break whenever the shipped cap moves.
      eligible_count: 4, max_gb: 100,
    },
  },
  messages: { welcome_text: "خوش آمدید", support_contact: "@support" },
  shop_state: { shop_closed: false, closed_text: null },
  channel: {
    channel_id: "@sample_channel",
    channel_link: "https://t.me/sample_channel",
    channel_required: true,
    verified: true,
    health: "ok",
  },
  config_version: 1,
};

type AdminPage = "plans" | "settings" | "managers" | "preview";

function renderAdmin(page: AdminPage, element: ReactElement) {
  return renderWithProviders(
    <Routes>
      <Route path="/portal/storefront/:shopId" element={<Outlet context={{ shop }} />}>
        <Route path={page} element={element} />
      </Route>
    </Routes>,
    `/portal/storefront/1/${page}`,
  );
}

function renderWithProviders(element: ReactElement, path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const rendered = render(
    <ThemeProvider theme={makeTheme("light")}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>{element}</MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return { ...rendered, queryClient };
}

/** Open «پلن جدید» and price it above the shop's floor (1 GB × 2,000), so tests that are about
 *  something else aren't blocked by the below-cost guard on the empty draft's 0 price. */
async function openCreateForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "پلن جدید" }));
  await user.type(screen.getByLabelText(/حجم \(گیگابایت\)/), "10");
  await user.type(screen.getByLabelText(/قیمت \(تومان\)/), "50000");
}

describe("storefront Release B administration", () => {
  it("uses one request for a double-click and never sends a plan title", async () => {
    const user = userEvent.setup();
    let requests = 0;
    let body: Record<string, unknown> = {};
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.post("*/api/portal/storefronts/1/plans", async ({ request }) => {
        requests += 1;
        body = await request.json() as Record<string, unknown>;
        await delay(60);
        return HttpResponse.json(
          { result: { id: 3, title: "", gb: 1, days: 30, price_toman: 0, enabled: true, sort_order: 0 }, config_version: 2 },
          { headers: { ETag: '"sf-config-2"' } },
        );
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await openCreateForm(user);
    await user.dblClick(screen.getByRole("button", { name: "ذخیره" }));

    await waitFor(() => expect(requests).toBe(1));
    expect(body).not.toHaveProperty("title");
    expect(await screen.findByText("تغییرات ذخیره شد.")).toBeInTheDocument();
  });

  it("reuses the same idempotency key after an ambiguous network failure", async () => {
    const user = userEvent.setup();
    const keys: string[] = [];
    let attempts = 0;
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.post("*/api/portal/storefronts/1/plans", ({ request }) => {
        attempts += 1;
        keys.push(request.headers.get("Idempotency-Key") || "");
        if (attempts === 1) return HttpResponse.error();
        return HttpResponse.json(
          { result: { id: 3, title: "", gb: 1, days: 30, price_toman: 0, enabled: true, sort_order: 0 }, config_version: 2 },
          { headers: { ETag: '"sf-config-2"' } },
        );
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await openCreateForm(user);
    await user.click(screen.getByRole("button", { name: "ذخیره" }));
    expect(await screen.findByText(/ذخیرهٔ تغییرات انجام نشد/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ذخیره" }));

    await waitFor(() => expect(attempts).toBe(2));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("reuses the same idempotency key after an unclassified HTTP 500", async () => {
    const user = userEvent.setup();
    const keys: string[] = [];
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.post("*/api/portal/storefronts/1/plans", ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") || "");
        if (keys.length === 1) return HttpResponse.json({ detail: "post-commit response failed" }, { status: 500 });
        return HttpResponse.json(
          { result: { id: 3, title: "", gb: 1, days: 30, price_toman: 0, enabled: true, sort_order: 0 }, config_version: 2 },
          { headers: { ETag: '"sf-config-2"' } },
        );
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await openCreateForm(user);
    await user.click(screen.getByRole("button", { name: "ذخیره" }));
    expect(await screen.findByText(/ذخیرهٔ تغییرات انجام نشد/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ذخیره" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).toBe(keys[0]);
  });

  it("reloads a stale ETag on config conflict and reapplies with a new idempotency key", async () => {
    const user = userEvent.setup();
    let reads = 0;
    let writes = 0;
    const etags: string[] = [];
    const keys: string[] = [];
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => {
        reads += 1;
        const version = reads === 1 ? 1 : 2;
        return HttpResponse.json({ items: plans, config_version: version }, { headers: { ETag: `"sf-config-${version}"` } });
      }),
      http.put("*/api/portal/storefronts/1/plans/1/enabled", ({ request }) => {
        writes += 1;
        etags.push(request.headers.get("If-Match") || "");
        keys.push(request.headers.get("Idempotency-Key") || "");
        if (writes === 1) return HttpResponse.json({ detail: { code: "config_conflict", current_version: 2 } }, { status: 409 });
        return HttpResponse.json({ result: { ...plans[0], enabled: false }, config_version: 3 }, { headers: { ETag: '"sf-config-3"' } });
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    const toggle = (await screen.findAllByRole("checkbox", { name: "فعال" }))[0];
    await user.click(toggle);
    expect(await screen.findByText("اطلاعات فروشگاه تغییر کرده است")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "بارگذاری و اعمال دوباره" }));

    await waitFor(() => expect(writes).toBe(2));
    expect(etags).toEqual(['"sf-config-1"', '"sf-config-2"']);
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("does not offer blind reapply for non-version 409 outcomes", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [plans[0]], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.put("*/api/portal/storefronts/1/plans/1/enabled", () => HttpResponse.json(
        { detail: { code: "unknown", message: "requires reconciliation" } },
        { status: 409 },
      )),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await user.click(await screen.findByRole("checkbox", { name: "فعال" }));
    expect(await screen.findByText(/نتیجهٔ عملیات قبلی نامشخص است/)).toBeInTheDocument();
    expect(screen.queryByText("اطلاعات فروشگاه تغییر کرده است")).not.toBeInTheDocument();
  });

  it.each(["in_flight", "unknown"])("preserves the command key when retrying a 409 %s outcome", async (code) => {
    const user = userEvent.setup();
    const keys: string[] = [];
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [plans[0]], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.put("*/api/portal/storefronts/1/plans/1/enabled", ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") || "");
        return HttpResponse.json({ detail: { code } }, { status: 409 });
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    const toggle = await screen.findByRole("checkbox", { name: "فعال" });
    await user.click(toggle);
    expect(await screen.findByText(code === "unknown" ? /نتیجهٔ عملیات قبلی نامشخص است/ : /عملیات قبلی هنوز در حال اجراست/)).toBeInTheDocument();
    await user.click(toggle);
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).toBe(keys[0]);
  });

  it("keeps the conflict dialog open when loading the fresh version fails", async () => {
    const user = userEvent.setup();
    let reads = 0;
    let writes = 0;
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => {
        reads += 1;
        if (reads > 1) return HttpResponse.json({ detail: "offline" }, { status: 503 });
        return HttpResponse.json({ items: [plans[0]], config_version: 1 }, { headers: { ETag: '"sf-config-1"' } });
      }),
      http.put("*/api/portal/storefronts/1/plans/1/enabled", () => {
        writes += 1;
        return HttpResponse.json({ detail: { code: "config_conflict", current_version: 2 } }, { status: 409 });
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await user.click(await screen.findByRole("checkbox", { name: "فعال" }));
    await user.click(await screen.findByRole("button", { name: "بارگذاری و اعمال دوباره" }));

    expect(await screen.findByText(/نسخهٔ تازه بارگذاری نشد/)).toBeInTheDocument();
    expect(screen.getByText("اطلاعات فروشگاه تغییر کرده است")).toBeInTheDocument();
    expect(writes).toBe(1);
  });

  it("plain conflict reload discards a stale plan edit instead of reapplying it", async () => {
    const user = userEvent.setup();
    let reads = 0;
    let writes = 0;
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => {
        reads += 1;
        const item = reads === 1 ? plans[0] : { ...plans[0], gb: 12 };
        return HttpResponse.json({ items: [item], config_version: reads === 1 ? 1 : 2 }, {
          headers: { ETag: reads === 1 ? '"sf-config-1"' : '"sf-config-2"' },
        });
      }),
      http.patch("*/api/portal/storefronts/1/plans/1", () => {
        writes += 1;
        return HttpResponse.json({ detail: { code: "config_conflict", current_version: 2 } }, { status: 409 });
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await user.click(await screen.findByRole("button", { name: "ویرایش پلن" }));
    const gb = await screen.findByLabelText(/حجم \(گیگابایت\)/);
    await user.clear(gb);
    await user.type(gb, "11");
    await user.click(screen.getByRole("button", { name: "ذخیره" }));
    await user.click(await screen.findByRole("button", { name: "بارگذاری نسخهٔ جدید" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "ویرایش پلن" })).not.toBeInTheDocument());
    expect(await screen.findByText(/۱۲ گیگ/)).toBeInTheDocument();
    expect(writes).toBe(1);
  });

  it("sends an exact full plan order through the accessible move controls", async () => {
    const user = userEvent.setup();
    let order: number[] = [];
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: plans, config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.put("*/api/portal/storefronts/1/plans/order", async ({ request }) => {
        order = ((await request.json()) as { plan_ids: number[] }).plan_ids;
        return HttpResponse.json({ result: [...plans].reverse(), config_version: 2 }, { headers: { ETag: '"sf-config-2"' } });
      }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await user.click(await screen.findByRole("button", { name: "انتقال ۱۰ گیگابایت · ۳۰ روزه به پایین" }));
    await waitFor(() => expect(order).toEqual([2, 1]));
  });

  it("shows audited before/after plan history including price changes", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [plans[0]], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.get("*/api/portal/storefronts/1/plans/1/history", () => HttpResponse.json({ items: [{
        id: 10,
        action: "plan.update",
        actor_telegram_id: "100",
        actor_role: "owner",
        source: "portal",
        before: { price_toman: 90_000 },
        after: { price_toman: 100_000 },
        outcome: "succeeded",
        created_at: "2026-07-16T12:00:00Z",
      }] })),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await user.click(await screen.findByRole("button", { name: "تاریخچه پلن" }));
    expect(await screen.findByText(/قبل: ۹۰٬۰۰۰ تومان/)).toBeInTheDocument();
    expect(screen.getByText(/بعد: ۱۰۰٬۰۰۰ تومان/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "بستن" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /تاریخچهٔ پلن/ })).not.toBeInTheDocument());
  });

  it("validates and normalizes payment settings before sending If-Match", async () => {
    const user = userEvent.setup();
    let sentBody: Record<string, unknown> | null = null;
    let sentEtag = "";
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(settings, { headers: { ETag: '"sf-config-1"' } })),
      http.patch("*/api/portal/storefronts/1/settings/payment", async ({ request }) => {
        sentBody = await request.json() as Record<string, unknown>;
        sentEtag = request.headers.get("If-Match") || "";
        return HttpResponse.json({ result: sentBody, config_version: 2 }, { headers: { ETag: '"sf-config-2"' } });
      }),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    const section = (await screen.findByText("روش‌های پرداخت")).closest(".MuiCard-root") as HTMLElement;
    const save = within(section).getByRole("button", { name: "ذخیره" });
    expect(save).toBeDisabled();
    await user.type(within(section).getByLabelText("شماره کارت"), "۶۲۷۴۱۲۳۴۵۶۷۸۹۰۱۲");
    await user.type(within(section).getByLabelText("نام صاحب کارت"), "علی");
    await user.click(save);

    await waitFor(() => expect(sentBody).not.toBeNull());
    expect(sentBody).toMatchObject({ card_number: "6274123456789012", card_holder: "علی" });
    expect(sentEtag).toBe('"sf-config-1"');
    expect(screen.getByText(/آخرین وضعیت ثبت‌شده: تأییدشده/)).toBeInTheDocument();
    expect(screen.getByText(/بررسی زنده محسوب نمی‌شود/)).toBeInTheDocument();
  });

  it("rejects malformed card, wallet and channel values without silently rewriting them", async () => {
    const user = userEvent.setup();
    server.use(http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json({
      ...settings,
      payment: {
        ...settings.payment,
        card_number: "6274123456789012",
        card_holder: "علی",
      },
    }, { headers: { ETag: '"sf-config-1"' } })));

    renderAdmin("settings", <StorefrontSettingsPage />);
    const paymentSection = (await screen.findByText("روش‌های پرداخت")).closest(".MuiCard-root") as HTMLElement;
    const card = within(paymentSection).getByLabelText("شماره کارت");
    await user.clear(card);
    await user.type(card, "ABCD6274123456789012");
    expect(card).toHaveValue("ABCD6274123456789012");
    expect(within(paymentSection).getByRole("button", { name: "ذخیره" })).toBeDisabled();

    await user.click(within(paymentSection).getByRole("checkbox", { name: "USDT" }));
    await user.type(within(paymentSection).getByLabelText("آدرس USDT"), "not-a-bep20-address");
    expect(within(paymentSection).getByLabelText("آدرس USDT")).toHaveAttribute("aria-invalid", "true");

    const channel = screen.getByLabelText("شناسه یا نام کاربری کانال");
    await user.clear(channel);
    await user.type(channel, "@abc");
    expect(screen.getByRole("button", { name: "بررسی و ذخیره" })).toBeDisabled();
  });

  it("preserves an unsaved messages draft while another settings group refetches", async () => {
    const user = userEvent.setup();
    let reads = 0;
    let trialBody: Record<string, unknown> = {};
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => {
        reads += 1;
        return HttpResponse.json({
          ...settings,
          trial: { ...settings.trial, free_trial_gb: reads === 1 ? 1 : 2 },
          config_version: reads === 1 ? 1 : 2,
        }, { headers: { ETag: reads === 1 ? '"sf-config-1"' : '"sf-config-2"' } });
      }),
      http.patch("*/api/portal/storefronts/1/settings/trial", async ({ request }) => {
        trialBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ result: { ...settings.trial, free_trial_gb: 2 }, config_version: 2 }, { headers: { ETag: '"sf-config-2"' } });
      }),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    const welcome = await screen.findByLabelText("پیام خوش‌آمد");
    await user.clear(welcome);
    await user.type(welcome, "پیش‌نویس ذخیره‌نشده");
    const trialSection = screen.getByText("تست رایگان").closest(".MuiCard-root") as HTMLElement;
    const gb = within(trialSection).getByLabelText("حجم (گیگابایت)");
    await user.clear(gb);
    await user.type(gb, "2");
    await user.click(within(trialSection).getByRole("button", { name: "ذخیره" }));

    await waitFor(() => expect(reads).toBeGreaterThan(1));
    expect(trialBody).toEqual({ free_trial_gb: 2 });
    expect(screen.getByLabelText("پیام خوش‌آمد")).toHaveValue("پیش‌نویس ذخیره‌نشده");
  });

  it("plain conflict reload discards the stale settings-group draft", async () => {
    const user = userEvent.setup();
    let reads = 0;
    let writes = 0;
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => {
        reads += 1;
        return HttpResponse.json({
          ...settings,
          messages: { ...settings.messages, welcome_text: reads === 1 ? "نسخه قدیمی" : "نسخه مدیر دیگر" },
          config_version: reads === 1 ? 1 : 2,
        }, { headers: { ETag: reads === 1 ? '"sf-config-1"' : '"sf-config-2"' } });
      }),
      http.patch("*/api/portal/storefronts/1/settings/messages", () => {
        writes += 1;
        return HttpResponse.json({ detail: { code: "config_conflict", current_version: 2 } }, { status: 409 });
      }),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    const welcome = await screen.findByLabelText("پیام خوش‌آمد");
    await user.clear(welcome);
    await user.type(welcome, "پیش‌نویس من");
    const section = screen.getByText("پیام‌ها و پشتیبانی").closest(".MuiCard-root") as HTMLElement;
    await user.click(within(section).getByRole("button", { name: "ذخیره" }));
    await user.click(await screen.findByRole("button", { name: "بارگذاری نسخهٔ جدید" }));

    await waitFor(() => expect(screen.getByLabelText("پیام خوش‌آمد")).toHaveValue("نسخه مدیر دیگر"));
    expect(writes).toBe(1);
  });

  it("invalidates storefront discovery and preview after changing shop state", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(settings, { headers: { ETag: '"sf-config-1"' } })),
      http.patch("*/api/portal/storefronts/1/settings/shop-state", () => HttpResponse.json({
        result: { shop_closed: true, closed_text: null },
        config_version: 2,
      }, { headers: { ETag: '"sf-config-2"' } })),
    );

    const { queryClient } = renderAdmin("settings", <StorefrontSettingsPage />);
    queryClient.setQueryDefaults(storefrontQueryKeys.all, { gcTime: 60_000 });
    queryClient.setQueryData(storefrontQueryKeys.all, [shop]);
    queryClient.setQueryData(storefrontQueryKeys.preview(1), { welcome_text: "old" });
    const stateSection = (await screen.findByText("وضعیت فروشگاه")).closest(".MuiCard-root") as HTMLElement;
    await user.click(within(stateSection).getByRole("checkbox", { name: "فروشگاه باز است" }));
    await user.click(within(stateSection).getByRole("button", { name: "ذخیره" }));
    await screen.findByText("تنظیمات ذخیره شد.");

    expect(queryClient.getQueryState(storefrontQueryKeys.all)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(storefrontQueryKeys.preview(1))?.isInvalidated).toBe(true);
    confirm.mockRestore();
  });

  it("sends only the channel identifier for server-side verification", async () => {
    const user = userEvent.setup();
    let sentBody: Record<string, unknown> = {};
    let sentEtag = "";
    let sentKey = "";
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(settings, { headers: { ETag: '"sf-config-1"' } })),
      http.post("*/api/portal/storefronts/1/channel", async ({ request }) => {
        sentBody = await request.json() as Record<string, unknown>;
        sentEtag = request.headers.get("If-Match") || "";
        sentKey = request.headers.get("Idempotency-Key") || "";
        return HttpResponse.json({ result: settings.channel, config_version: 2 }, { headers: { ETag: '"sf-config-2"' } });
      }),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    const field = await screen.findByLabelText("شناسه یا نام کاربری کانال");
    await user.clear(field);
    await user.type(field, "@new_channel");
    await user.click(screen.getByRole("button", { name: "بررسی و ذخیره" }));

    await waitFor(() => expect(sentBody).toEqual({ channel_id: "@new_channel" }));
    expect(sentEtag).toBe('"sf-config-1"');
    expect(sentKey).toBeTruthy();
  });

  it("uses a new idempotency key when retrying a definitive channel 502", async () => {
    const user = userEvent.setup();
    const keys: string[] = [];
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(settings, { headers: { ETag: '"sf-config-1"' } })),
      http.post("*/api/portal/storefronts/1/channel", ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") || "");
        if (keys.length === 1) return HttpResponse.json({ detail: { code: "external_failure" } }, { status: 502 });
        return HttpResponse.json({ result: settings.channel, config_version: 2 }, { headers: { ETag: '"sf-config-2"' } });
      }),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    const save = await screen.findByRole("button", { name: "بررسی و ذخیره" });
    await user.click(save);
    expect(await screen.findByText(/ذخیره انجام نشد/)).toBeInTheDocument();
    await user.click(save);
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("shows owner-only managers and sends an audited add command", async () => {
    const user = userEvent.setup();
    let added = "";
    server.use(
      http.get("*/api/portal/storefronts/1/managers", () => HttpResponse.json({
        owner_id: 100,
        items: [
          { telegram_id: 100, role: "owner", removable: false },
          { telegram_id: 200, role: "manager", removable: true },
        ],
        max_count: 10,
        config_version: 1,
      }, { headers: { ETag: '"sf-config-1"' } })),
      http.post("*/api/portal/storefronts/1/managers", async ({ request }) => {
        added = String(((await request.json()) as { telegram_id: string }).telegram_id);
        return HttpResponse.json({ result: {
          owner_id: 100,
          items: [{ telegram_id: 100, role: "owner", removable: false }],
          max_count: 10,
          config_version: 2,
        }, config_version: 2 }, { headers: { ETag: '"sf-config-2"' } });
      }),
    );

    renderAdmin("managers", <StorefrontManagersPage />);
    expect(await screen.findByText("مالک اصلی")).toBeInTheDocument();
    expect(screen.getByText(/امکان ورود به پورتال ندارند/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("شناسهٔ تلگرامِ مدیر جدید"), "300");
    await user.click(screen.getByRole("button", { name: "افزودن مدیر" }));
    await waitFor(() => expect(added).toBe("300"));
  });

  it("round-trips and removes maximum int64 manager ids without JS rounding", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    let removed = "";
    const rawManagers = '{"owner_id":1,"items":[{"telegram_id":1,"role":"owner","removable":false},{"telegram_id":9223372036854775807,"role":"manager","removable":true}],"max_count":10,"config_version":1}';
    server.use(
      http.get("*/api/portal/storefronts/1/managers", () => new HttpResponse(rawManagers, {
        headers: { "Content-Type": "application/json", ETag: '"sf-config-1"' },
      })),
      http.delete("*/api/portal/storefronts/1/managers/:telegramId", ({ params }) => {
        removed = String(params.telegramId);
        return new HttpResponse(`{"result":${rawManagers},"config_version":2}`, {
          headers: { "Content-Type": "application/json", ETag: '"sf-config-2"' },
        });
      }),
    );

    renderAdmin("managers", <StorefrontManagersPage />);
    expect(await screen.findByText("9223372036854775807")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "حذف مدیر 9223372036854775807" }));
    await waitFor(() => expect(removed).toBe("9223372036854775807"));
    confirm.mockRestore();
  });

  it("renders the pure customer preview without invoking any mutation", async () => {
    server.use(http.get("*/api/portal/storefronts/1/preview", () => HttpResponse.json({
      welcome_text: "به فروشگاه خوش آمدید",
      support_contact: "@support",
      shop_closed: false,
      closed_text: null,
      free_trial: { enabled: true, gb: 2, days: 3 },
      payment_methods: ["card", "usdt"],
      enabled_plans: [{ id: 1, gb: 10, days: 30, price_toman: 100_000 }],
      channel_required: true,
    })));

    renderAdmin("preview", <StorefrontPreviewPage />);
    expect(await screen.findByText("به فروشگاه خوش آمدید")).toBeInTheDocument();
    expect(screen.getByText("۱۰ گیگابایت · ۳۰ روزه")).toBeInTheDocument();
    expect(screen.getByText("تست رایگان")).toBeInTheDocument();
    expect(screen.getByText(/هیچ مشتری یا داده‌ای ایجاد نمی‌کند/)).toBeInTheDocument();
  });

  it("renders tenant 404 as a non-leaking data error", async () => {
    server.use(http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json({ detail: "not found" }, { status: 404 })));
    renderAdmin("plans", <StorefrontPlansPage />);
    expect(await screen.findByText(/خطا در بارگذاری اطلاعات/)).toBeInTheDocument();
    expect(screen.queryByText("not found")).not.toBeInTheDocument();
  });

  it.each([
    ["mobile", false],
    ["desktop", true],
  ])("exposes the Release B nested tabs on %s", async (_label, desktop) => {
    setMatchMedia(desktop);
    server.use(
      http.get("*/api/portal/storefronts", () => HttpResponse.json([shop])),
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
    );
    renderWithProviders(
      <Routes>
        <Route path="/portal/storefront/:shopId" element={<StorefrontShell />}>
          <Route path="plans" element={<StorefrontPlansPage />} />
        </Route>
      </Routes>,
      "/portal/storefront/1/plans",
    );

    expect(await screen.findByRole("tab", { name: "پلن‌ها", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "تنظیمات" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "مدیران" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "پیش‌نمایش مشتری" })).toBeInTheDocument();
  });

  // A plan priced below the reseller's own cost (2,000 T/GB here) is a loss on every sale. The
  // overwhelmingly common cause is a unit slip — 50 typed for 50,000 — so the form has to teach
  // the unit, not just refuse the number.
  it("blocks a below-cost price in the plan form and spells out the unit", async () => {
    const user = userEvent.setup();
    let posted = 0;
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.post("*/api/portal/storefronts/1/plans", () => { posted += 1; return HttpResponse.json({}); }),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await user.click(await screen.findByRole("button", { name: "پلن جدید" }));
    const gb = screen.getByLabelText(/حجم \(گیگابایت\)/);
    await user.clear(gb);
    await user.type(gb, "10");
    const price = screen.getByLabelText(/قیمت \(تومان\)/);
    await user.clear(price);
    await user.type(price, "50");

    expect(screen.getByText(/برای ۵۰ هزار تومان بنویسید 50000/)).toBeInTheDocument();
    expect(screen.getByText(/کفِ مجاز برای این پلن/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "ذخیره" })).toBeDisabled();

    await user.clear(price);
    await user.type(price, "50000");
    expect(screen.getByRole("button", { name: "ذخیره" })).toBeEnabled();
    expect(posted).toBe(0);
  });

  it("renders the server's below-cost explanation verbatim, not the generic failure", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { items: [], config_version: 1 },
        { headers: { ETag: '"sf-config-1"' } },
      )),
      // The client floor and the server's may disagree (another admin just changed the price),
      // so the 422 still has to surface usefully.
      http.post("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        { detail: { code: "below_cost", message: "⛔️ این قیمت از هزینهٔ خودِ شما کمتر است و ذخیره نشد." } },
        { status: 422 },
      )),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    await openCreateForm(user);
    await user.click(screen.getByRole("button", { name: "ذخیره" }));

    expect(await screen.findByText(/این قیمت از هزینهٔ خودِ شما کمتر است/)).toBeInTheDocument();
    expect(screen.queryByText(/ذخیرهٔ تغییرات انجام نشد/)).not.toBeInTheDocument();
  });

  it("will not let a below-cost plan be switched back on, and flags the ones that are", async () => {
    server.use(
      http.get("*/api/portal/storefronts/1/plans", () => HttpResponse.json(
        {
          items: [
            { id: 1, gb: 10, days: 30, price_toman: 50, enabled: false, sort_order: 0 },
            { id: 2, gb: 10, days: 30, price_toman: 50_000, enabled: true, sort_order: 1 },
          ],
          config_version: 1,
        },
        { headers: { ETag: '"sf-config-1"' } },
      )),
    );

    renderAdmin("plans", <StorefrontPlansPage />);
    expect(await screen.findByText(/قیمتِ ۱ پلن از هزینهٔ خودتان کمتر است/)).toBeInTheDocument();

    const switches = screen.getAllByRole("checkbox");
    expect(switches[0]).toBeDisabled();     // below cost → cannot be re-enabled
    expect(switches[1]).toBeEnabled();      // healthy → still freely toggleable
  });
});

describe("storefront monthly free-trial reset", () => {
  const settingsWithReset = (reset: Record<string, unknown>) => ({
    ...settings, trial: { ...settings.trial, reset: { ...settings.trial.reset, ...reset } },
  });

  it("reports the automatic monthly re-arm and offers no way to trigger it", async () => {
    // The trigger was removed from both the portal and the bot: it ran far too rarely while it
    // depended on a shop admin pressing something, so the platform re-arms every shop on a
    // schedule. A leftover button would consume the month's single reset and make the sweep skip
    // the shop, for an announcement that was going out anyway.
    let posted = 0;
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(
        settingsWithReset({ eligible_count: 12, last_reset_period: "2026-08" }),
        { headers: { ETag: '"sf-config-1"' } },
      )),
      http.post("*/api/portal/storefronts/1/settings/trial/reset", () => {
        posted += 1;
        return HttpResponse.json({ result: {}, config_version: 2 });
      }),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    expect(await screen.findByText(/ابتدای هر ماه میلادی/)).toBeInTheDocument();
    expect(screen.getByText(/آخرین فعال‌سازی: دورهٔ 2026-08/)).toBeInTheDocument();
    // No trigger of any shape — not a disabled one, not a confirm dialog.
    expect(screen.queryByRole("button", { name: /ریست/ })).toBeNull();
    expect(screen.queryByText(/قابل بازگشت نیست/)).toBeNull();
    expect(posted).toBe(0);
  });

  it("says the trial is one-per-customer when the owner switched the re-arm off", async () => {
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(
        settingsWithReset({ available: false, reason: "disabled", last_reset_period: null }),
        { headers: { ETag: '"sf-config-1"' } },
      )),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    expect(await screen.findByText(/یک‌بار است و به‌صورت خودکار تمدید نمی‌شود/)).toBeInTheDocument();
    expect(screen.queryByText(/ابتدای هر ماه میلادی/)).toBeNull();
  });

  it("caps the trial size field at the owner's ceiling, not a constant", async () => {
    server.use(
      http.get("*/api/portal/storefronts/1/settings", () => HttpResponse.json(
        settingsWithReset({ max_gb: 1 }), { headers: { ETag: '"sf-config-1"' } },
      )),
    );

    renderAdmin("settings", <StorefrontSettingsPage />);
    const gb = await screen.findByLabelText("حجم (گیگابایت)");
    expect(gb).toHaveAttribute("inputmode", "numeric");
    expect(screen.getByText("حداکثر 1 گیگابایت")).toBeInTheDocument();
    await userEvent.setup().type(gb, "5");
    expect(gb).toHaveAttribute("aria-invalid", "true");
  });
});
