import axios from "axios";

// Same base resolution as the owner client: undefined (local dev) → :8000, "" (prod build) →
// same-origin (Caddy proxies /api). The portal is a separate axios instance with its OWN token
// so a reseller session and an owner session never collide in one browser.
const _envBase = (import.meta as any).env?.VITE_API_BASE_URL;
const baseURL = _envBase === undefined ? "http://localhost:8000" : _envBase;

export const portalApi = axios.create({ baseURL, timeout: 120000 });

const PORTAL_TOKEN_KEY = "portal_token";

export const getPortalToken = () => localStorage.getItem(PORTAL_TOKEN_KEY);
export const setPortalToken = (t: string | null) => {
  if (t) localStorage.setItem(PORTAL_TOKEN_KEY, t);
  else localStorage.removeItem(PORTAL_TOKEN_KEY);
};

portalApi.interceptors.request.use((config) => {
  const t = getPortalToken();
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

portalApi.interceptors.response.use(
  (r) => r,
  (err) => {
    // A 401 means the reseller session expired/was revoked → drop it and bounce to the
    // login page (which tells them to re-tap the bot button). Never touch the owner token.
    if (err?.response?.status === 401 && getPortalToken()) {
      setPortalToken(null);
      if (!location.pathname.startsWith("/portal/login")) location.href = "/portal/login";
    }
    return Promise.reject(err);
  }
);

// ---- auth ----
export const portalExchange = (token: string) =>
  portalApi.post("/api/portal/auth/exchange", { token }).then((r) => r.data as { access_token: string });

export interface PortalReseller {
  id: number;
  name: string;
  admin_uuid: string;
  panel_key: string;
  link_tag: string | null;
  enforcement_state: string;
}
export const portalMe = () =>
  portalApi.get("/api/portal/me").then((r) => r.data as { chat_id: number; resellers: PortalReseller[] });

// ---- dashboard ----
export interface PortalSummary {
  period: string;
  estimate: { amount_toman: number; gb: number; users: number };
  per_reseller: { id: number; name: string; panel_key: string; amount_toman: number; gb: number; users: number }[];
  outstanding: { amount_toman: number; count: number };
  reseller_count: number;
  trend: { day: number; date: string; amount_toman: number }[];
}
export const portalSummary = (period?: string) =>
  portalApi.get("/api/portal/summary", { params: { period } }).then((r) => r.data as PortalSummary);

// ---- invoices ----
export interface PortalInvoice {
  id: number;
  number: string;
  period_label: string;
  panel_key: string;
  usage_gb: number;
  amount_toman: number;
  status: string;
  owed: boolean;
  deferred_until: string | null;
  created_at: string | null;
}
export const portalInvoices = () =>
  portalApi.get("/api/portal/invoices").then((r) => r.data as PortalInvoice[]);

// Fetch the PDF WITH the auth header (a plain link would 401), then open it in a new tab.
export async function openPortalInvoicePdf(invoiceId: number) {
  const res = await portalApi.get(`/api/portal/invoices/${invoiceId}/pdf`, { responseType: "blob" });
  const url = URL.createObjectURL(res.data as Blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// ---- payments ----
export interface PortalPayment {
  id: number;
  number: string;
  method: string;
  status: string;
  chain: string | null;
  txid: string | null;
  amount_toman: number;
  invoice_period: string | null;   // joined «دوره۱، دوره۲» when the payment covers several
  invoice_count: number;
  has_proof: boolean;
  created_at: string | null;
  verified_at: string | null;
}
export const portalPayments = () =>
  portalApi.get("/api/portal/payments").then((r) => r.data as PortalPayment[]);

// ---- panels ----
export interface PortalPanel {
  reseller_id: number;
  name: string;
  panel_key: string;
  panel_name: string;
  link: string;
  previous_link: string | null;
}
export const portalPanels = () =>
  portalApi.get("/api/portal/panels").then((r) => r.data as PortalPanel[]);

// ---- sub-resellers ----
export interface PortalSub {
  id: number;
  name: string;
  panel_key: string;
  parent_name: string;
  users: number;
  enabled_users: number;
  max_users: number | null;
  can_add_admin: boolean;
  enforcement_state: string;
  gb_cap: number | null;
  current_gb: number;
  cap_pct: number | null;
  this_month_amount: number;
  months: { label: string; amount_toman: number; gb: number }[];
}
export const portalSubs = () =>
  portalApi.get("/api/portal/subs").then((r) => r.data as PortalSub[]);

// Download a sub-reseller's GB-only invoice PDF for a period (auth header required → fetch as blob).
export async function openPortalSubPdf(subId: number, period: string) {
  const res = await portalApi.get(`/api/portal/subs/${subId}/pdf`, {
    params: { period }, responseType: "blob",
  });
  const url = URL.createObjectURL(res.data as Blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
export const portalSubSalesByDay = (subId: number, period?: string) =>
  portalApi.get(`/api/portal/subs/${subId}/sales-by-day`, { params: { period } })
    .then((r) => r.data as { day: number; date: string; amount_toman: number }[]);

export const portalBumpSub = (subId: number, amount: number) =>
  portalApi.post(`/api/portal/subs/${subId}/bump-limits`, { amount })
    .then((r) => r.data as { max_users: number; max_active_users: number });
export const portalSubCanAddAdmin = (subId: number, enabled: boolean) =>
  portalApi.post(`/api/portal/subs/${subId}/can-add-admin`, { enabled })
    .then((r) => r.data as { ok: boolean; can_add_admin: boolean });

// ---- my capacity ----
export interface PortalCapacity {
  reseller_id: number;
  name: string;
  panel_key: string;
  used: number;
  max: number | null;
  can_add_admin: boolean;
}
export const portalCapacity = () =>
  portalApi.get("/api/portal/capacity").then((r) => r.data as PortalCapacity[]);
export const portalRequestCapacity = (body: { reseller_id: number; amount?: number; note?: string }) =>
  portalApi.post("/api/portal/capacity/request", body).then((r) => r.data as { ok: boolean });

// ---- payment proof ----
export async function openPortalPaymentProof(paymentId: number) {
  const res = await portalApi.get(`/api/portal/payments/${paymentId}/proof`, { responseType: "blob" });
  const url = URL.createObjectURL(res.data as Blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// ---- notifications (derived feed) ----
export interface PortalNotification {
  key: string;
  type: string;
  at: string | null;
  title: string;
  severity: "info" | "success" | "warning" | "error";
}
export const portalNotifications = () =>
  portalApi.get("/api/portal/notifications").then((r) => r.data as { events: PortalNotification[] });

// ---- actions (P2) ----
export interface PayOptions {
  invoice: { id: number; number: string; period_label: string; amount_toman: number; amount_usdt: number };
  payable: boolean;
  pending: boolean;
  methods: {
    usdt: boolean; card: boolean; ton: boolean; avax: boolean; screenshot: boolean;
    wallet: string; card_number: string; card_holder: string; ton_address: string;
    avax_address: string;
    amount_ton: number | null;
    amount_avax: number | null;
  };
}
export const portalPayOptions = (invoiceId: number) =>
  portalApi.get("/api/portal/pay/options", { params: { invoice_id: invoiceId } })
    .then((r) => r.data as PayOptions);

// All payable invoices + summed totals + methods, for the «pay all» dialog (one transfer
// settles every payable invoice). Shaped like PayOptions but with a list + totals.
export interface PayOptionsAll {
  invoices: { id: number; number: string; period_label: string; amount_toman: number; amount_usdt: number }[];
  invoice_ids: number[];
  count: number;
  total_amount_toman: number;
  total_amount_usdt: number;
  methods: PayOptions["methods"];
}
export const portalPayOptionsAll = () =>
  portalApi.get("/api/portal/pay/options-all").then((r) => r.data as PayOptionsAll);

export interface PaySubmitResult { status: string; message: string; number: string | null }
export const portalPayTxid = (
  body: { invoice_id?: number; invoice_ids?: number[]; txid: string; chain: string },
) => portalApi.post("/api/portal/pay/txid", body).then((r) => r.data as PaySubmitResult);
export const portalPayScreenshot = (invoiceIds: number[], file: File) => {
  const fd = new FormData();
  fd.append("invoice_ids", invoiceIds.join(","));
  fd.append("file", file);
  return portalApi
    .post("/api/portal/pay/screenshot", fd, { headers: { "Content-Type": "multipart/form-data" } })
    .then((r) => r.data as PaySubmitResult);
};

export const portalSetSubCap = (subId: number, gb: number) =>
  portalApi.post(`/api/portal/subs/${subId}/cap`, { gb }).then((r) => r.data as { ok: boolean; gb_cap: number | null });
export const portalSuspendSub = (subId: number) =>
  portalApi.post(`/api/portal/subs/${subId}/suspend`).then((r) => r.data as { status: string; error: string | null });
export const portalFreezeSub = (subId: number) =>
  portalApi.post(`/api/portal/subs/${subId}/freeze`).then((r) => r.data as { status: string; error: string | null });
export const portalRestoreSub = (subId: number) =>
  portalApi.post(`/api/portal/subs/${subId}/restore`).then((r) => r.data as { status: string; error: string | null });

export const portalSupport = (text: string) =>
  portalApi.post("/api/portal/support", { text }).then((r) => r.data as { ok: boolean });
