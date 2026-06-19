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
  number: string;
  method: string;
  status: string;
  chain: string | null;
  txid: string | null;
  amount_toman: number;
  invoice_period: string | null;
  created_at: string | null;
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
  enforcement_state: string;
  gb_cap: number | null;
  current_gb: number;
  cap_pct: number | null;
  this_month_amount: number;
}
export const portalSubs = () =>
  portalApi.get("/api/portal/subs").then((r) => r.data as PortalSub[]);

// ---- actions (P2) ----
export interface PayOptions {
  invoice: { id: number; number: string; period_label: string; amount_toman: number; amount_usdt: number };
  payable: boolean;
  pending: boolean;
  methods: {
    usdt: boolean; card: boolean; ton: boolean; screenshot: boolean;
    wallet: string; card_number: string; card_holder: string; ton_address: string;
    amount_ton: number | null;
  };
}
export const portalPayOptions = (invoiceId: number) =>
  portalApi.get("/api/portal/pay/options", { params: { invoice_id: invoiceId } })
    .then((r) => r.data as PayOptions);

export interface PaySubmitResult { status: string; message: string; number: string | null }
export const portalPayTxid = (body: { invoice_id: number; txid: string; chain: string }) =>
  portalApi.post("/api/portal/pay/txid", body).then((r) => r.data as PaySubmitResult);
export const portalPayScreenshot = (invoiceId: number, file: File) => {
  const fd = new FormData();
  fd.append("invoice_id", String(invoiceId));
  fd.append("file", file);
  return portalApi
    .post("/api/portal/pay/screenshot", fd, { headers: { "Content-Type": "multipart/form-data" } })
    .then((r) => r.data as PaySubmitResult);
};

export const portalSetSubCap = (subId: number, gb: number) =>
  portalApi.post(`/api/portal/subs/${subId}/cap`, { gb }).then((r) => r.data as { ok: boolean; gb_cap: number | null });
export const portalSuspendSub = (subId: number) =>
  portalApi.post(`/api/portal/subs/${subId}/suspend`).then((r) => r.data as { status: string; error: string | null });
export const portalRestoreSub = (subId: number) =>
  portalApi.post(`/api/portal/subs/${subId}/restore`).then((r) => r.data as { status: string; error: string | null });

export const portalSupport = (text: string) =>
  portalApi.post("/api/portal/support", { text }).then((r) => r.data as { ok: boolean });
