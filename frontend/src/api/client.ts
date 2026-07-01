import axios from "axios";

import { queryClient } from "./queryClient";

// VITE_API_BASE_URL:
//   • undefined (local dev, var not set)  → talk to the dev backend on :8000
//   • "" (production build arg)            → same-origin (Caddy proxies /api). MUST
//     stay "" — NOT fall back to localhost, or the browser would call the wrong host.
const _envBase = (import.meta as any).env?.VITE_API_BASE_URL;
const baseURL = _envBase === undefined ? "http://localhost:8000" : _envBase;

export const api = axios.create({ baseURL, timeout: 120000 });

const TOKEN_KEY = "invoice_token";

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (t: string | null) => {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
};

api.interceptors.request.use((config) => {
  const t = getToken();
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401 && getToken()) {
      setToken(null);
      // Drop the whole react-query cache: after a re-login the app must refetch, not
      // serve pre-logout invoice/payment data (possibly another era of the account).
      queryClient.clear();
      if (!location.pathname.startsWith("/login")) location.href = "/login";
    }
    return Promise.reject(err);
  }
);

// Fetch the PDF WITH the auth header (a plain link would 401), then open it.
export async function openInvoicePdf(invoiceId: number) {
  const res = await api.get(`/api/invoices/${invoiceId}/pdf`, { responseType: "blob" });
  const url = URL.createObjectURL(res.data as Blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// ---- meta ----
export const getInfo = () =>
  api.get("/api/info").then((r) => r.data as { name: string; version: string; env: string });

// ---- setup (first-run wizard) ----
export const getSetupStatus = () =>
  api.get("/api/setup/status").then((r) => r.data as { setup_done: boolean; domain: string; https_enabled: boolean });
export const doSetup = (body: { username: string; password: string; domain?: string; acme_email?: string }) =>
  api.post("/api/setup", body).then((r) => r.data);

// ---- auth ----
export const getCaptcha = () =>
  api.get("/api/auth/captcha").then((r) => r.data as { captcha_id: string; image: string });

export async function login(payload: {
  username: string; password: string; captcha_id: string; captcha_answer: string; totp_code?: string;
}) {
  const { data } = await api.post("/api/auth/login", payload);
  return data as { access_token: string };
}
export const getMe = () => api.get("/api/auth/me").then((r) => r.data);

// ---- Passkey (WebAuthn / Face ID) ----
export const passkeyRegisterBegin = () =>
  api.post("/api/auth/passkey/register/begin").then((r) => r.data as { handle: string; options: any });
export const passkeyRegisterComplete = (body: { handle: string; credential: any; name?: string }) =>
  api.post("/api/auth/passkey/register/complete", body).then((r) => r.data);
export const passkeyLoginBegin = () =>
  api.post("/api/auth/passkey/login/begin").then((r) => r.data as { handle: string; options: any });
export const passkeyLoginComplete = (body: { handle: string; credential: any }) =>
  api.post("/api/auth/passkey/login/complete", body).then((r) => r.data as { access_token: string });
export const passkeyList = () =>
  api.get("/api/auth/passkey/list").then((r) => r.data as { id: number; name: string; created_at: string }[]);
export const passkeyDelete = (id: number) =>
  api.delete(`/api/auth/passkey/${id}`).then((r) => r.data);

// ---- 2FA ----
export const totpSetup = () => api.post("/api/auth/2fa/setup").then((r) => r.data);
export const totpEnable = (code: string) => api.post("/api/auth/2fa/enable", { code }).then((r) => r.data);
export const totpDisable = (current_password: string) =>
  api.post("/api/auth/2fa/disable", { current_password }).then((r) => r.data);

// ---- panels ----
export const listPanels = () => api.get("/api/panels").then((r) => r.data);
export const createPanel = (b: any) => api.post("/api/panels", b).then((r) => r.data);
export const updatePanel = (id: number, b: any) => api.patch(`/api/panels/${id}`, b).then((r) => r.data);
export const deletePanel = (id: number) => api.delete(`/api/panels/${id}`);
export const syncPanel = (id: number) => api.post(`/api/panels/${id}/sync`).then((r) => r.data);
export const syncAllPanels = () => api.post("/api/panels/sync-all").then((r) => r.data);
export const testPanel = (id: number) => api.post(`/api/panels/${id}/test`).then((r) => r.data);

// ---- resellers ----
export interface ResellerRow {
  id: number;
  panel_id: number;
  panel_key: string;
  admin_uuid: string;
  name: string;
  parent_admin_uuid: string | null;
  mode: string;
  is_owner: boolean;
  comment: string | null;
  exclude_from_billing: boolean;
  price_per_gb: number | null;
  effective_price_per_gb: number;
  min_sale_toman: number | null;
  bot_chat_id: number | null;
  username: string | null;
  panel_telegram_id: number | null;
  link_tag: string | null;
  registered: boolean;
  enforcement_state: string;
  panel_max_users: number | null;
  panel_max_active_users: number | null;
  can_add_admin: boolean;
  storefront_enabled: boolean;
  storefront_monthly_fee_toman: number | null;
  users_count: number;
  active_users_count: number;
  capacity_pct: number;
  last_seen_at: string | null;
}

export interface ResellerTreeRow extends ResellerRow {
  children: ResellerTreeRow[];
  descendant_count: number;
  cycle_detected: boolean;
}

export const listResellers = (params: any = {}) =>
  api.get("/api/resellers", { params }).then((r) => r.data as ResellerRow[]);
export const getResellerTree = (params: any = {}) =>
  api.get("/api/resellers/tree", { params }).then((r) => r.data as ResellerTreeRow[]);

export interface AbsentReseller {
  id: number;
  panel_id: number;
  panel_key: string;
  admin_uuid: string;
  name: string;
  last_seen_at: string | null;
  users_count: number;
  sub_resellers: number;
  has_nondraft_invoices: boolean;
  has_payments: boolean;
}
export const listAbsentResellers = (params: { panel_id?: number } = {}) =>
  api.get("/api/resellers/absent", { params }).then((r) => r.data as AbsentReseller[]);
export const deleteAbsentReseller = (id: number) =>
  api.delete(`/api/resellers/${id}/absent`).then((r) => r.data);
export const updateReseller = (id: number, b: any) =>
  api.patch(`/api/resellers/${id}`, b).then((r) => r.data);
export const enforceReseller = (id: number, dry_run?: boolean) =>
  api.post(`/api/resellers/${id}/enforce`, null, { params: { dry_run } }).then((r) => r.data);
export const restoreReseller = (id: number) =>
  api.post(`/api/resellers/${id}/restore`).then((r) => r.data);
export const bumpResellerLimits = (id: number, amount: number) =>
  api.post(`/api/resellers/${id}/bump-limits`, { amount }).then((r) => r.data);
export const setResellerCanAddAdmin = (id: number, enabled: boolean) =>
  api.post(`/api/resellers/${id}/can-add-admin`, { enabled }).then((r) => r.data);
export const unbindResellerTelegram = (id: number) =>
  api.post(`/api/resellers/${id}/unbind-telegram`).then((r) => r.data);

// ---- tools (rare/special owner operations) ----
export interface ToolsEndUser {
  id: number;
  panel_id: number;
  panel_key: string;
  user_uuid: string;
  name: string;
  added_by_uuid: string | null;
  reseller_name: string;
  usage_limit_gb: number;
  current_usage_gb: number;
  start_date: string | null;
  enable: boolean;
  present_on_panel: boolean;
}
export const searchEndUsers = (q: string) =>
  api.get("/api/tools/end-users", { params: { q } }).then((r) => r.data as ToolsEndUser[]);
export const removeEndUser = (id: number) =>
  api.post(`/api/tools/end-users/${id}/remove`).then((r) => r.data);

export interface AdminDeleteScope {
  reseller_id: number;
  name: string;
  admin_uuid: string;
  panel_key: string;
  is_owner: boolean;
  sub_reseller_count: number;
  user_count: number;
}
export const previewAdminDelete = (id: number) =>
  api.get(`/api/tools/admin/${id}/delete-preview`).then((r) => r.data as AdminDeleteScope);
export const deleteAdminCascade = (id: number) =>
  api.post(`/api/tools/admin/${id}/delete`).then((r) => r.data as AdminDeleteScope & { queued: boolean; action_id: number });

// ---- invoices ----
export interface InvoiceListItem {
  id: number;
  number: string;
  reseller_id: number;
  reseller_name: string;
  reseller_chat_id: number | null;
  reseller_username: string | null;
  panel_id: number;
  panel_key: string;
  period_label: string;
  period_start: string;
  period_end: string;
  usage_gb: number;
  users_count: number;
  price_per_gb: number;
  amount_toman: number;
  base_amount_toman: number;
  min_sale_toman: number;
  floor_applied: boolean;
  status: string;
  sent_at: string | null;
  paid_at: string | null;
  deferred_until: string | null;
  defer_note: string | null;
  created_at: string | null;
}

export const listInvoices = (params: any = {}) =>
  api.get("/api/invoices", { params }).then((r) => r.data as InvoiceListItem[]);
export const getInvoice = (id: number) => api.get(`/api/invoices/${id}`).then((r) => r.data);
export const generateInvoices = (b: any) => api.post("/api/invoices/generate", b).then((r) => r.data);
export const discardDrafts = (period?: string) =>
  api.post("/api/invoices/discard-drafts", null, { params: { period } }).then((r) => r.data);
export const sendInvoice = (id: number) => api.post(`/api/invoices/${id}/send`).then((r) => r.data);
export const sendPeriod = (period: string) =>
  api.post("/api/invoices/send-period", null, { params: { period } }).then((r) => r.data);
export const markInvoicePaid = (id: number) => api.post(`/api/invoices/${id}/mark-paid`).then((r) => r.data);
export const unmarkInvoicePaid = (id: number) => api.post(`/api/invoices/${id}/unmark-paid`).then((r) => r.data);
export const editInvoice = (id: number, body: any) => api.patch(`/api/invoices/${id}`, body).then((r) => r.data);
export const recomputeInvoice = (id: number) => api.post(`/api/invoices/${id}/recompute`).then((r) => r.data);
export const revertInvoiceToDraft = (id: number) => api.post(`/api/invoices/${id}/revert-to-draft`).then((r) => r.data);
export const deferInvoice = (id: number, body: { deferred_until: string | null; defer_note?: string }) =>
  api.post(`/api/invoices/${id}/defer`, body).then((r) => r.data);

// ---- payments ----
export const listPayments = (params: any = {}) =>
  api.get("/api/payments", { params }).then((r) => r.data);
export const verifyPayment = (id: number) => api.post(`/api/payments/${id}/verify`).then((r) => r.data);
export const confirmPayment = (id: number) =>
  api.post(`/api/payments/${id}/confirm`).then((r) => r.data);
export const rejectPayment = (id: number) => api.post(`/api/payments/${id}/reject`).then((r) => r.data);
export const deletePayment = (id: number) => api.delete(`/api/payments/${id}`).then((r) => r.data);
// Manual-confirm aid: the actual on-chain deposit (TON or USDT/BEP-20) vs the invoice amount.
export const depositCheck = (id: number) =>
  api.get(`/api/payments/${id}/deposit-check`).then((r) => r.data);
export const refreshRate = () => api.post("/api/ops/refresh-rate").then((r) => r.data);

export interface RatesInfo {
  mode: "auto" | "manual";
  source: "wallex" | "tetherland";
  effective: number;
  usdt_auto: number;
  usdt_auto_at: string;
  usdt_manual: number;
  ton_mode: "auto" | "manual";
  ton_effective: number;
  ton_auto: number;
  ton_manual: number;
  ton_enabled: boolean;
  stale: boolean;
}
export const getRates = (): Promise<RatesInfo> =>
  api.get("/api/ops/rates").then((r) => r.data);
// Fetch the deposit screenshot (authenticated) as a blob and open it in a new tab.
export const openPaymentProof = async (id: number) => {
  const r = await api.get(`/api/payments/${id}/proof`, { responseType: "blob" });
  const url = URL.createObjectURL(r.data);
  window.open(url, "_blank");
};

// ---- reports ----
export interface DashboardPanelSales {
  panel_id: number;
  panel_key: string;
  invoices: number;
  usage_gb: number;
  amount_toman: number;
}

export interface DashboardSalesRow {
  invoice_id: number;
  reseller_id: number;
  reseller_name: string;
  panel_key: string;
  usage_gb: number;
  amount_toman: number;
  status: string;
}

export interface DashboardSummary {
  period: string;
  previous_period: string;
  panels: number;
  active_panels: number;
  healthy_panels: number;
  resellers: number;
  billable_resellers: number;
  registered_resellers: number;
  invoices_total: number;
  period_invoices: number;
  period_billed_toman: number;
  previous_period_billed_toman: number;
  period_paid_toman: number;
  outstanding_toman: number;
  outstanding_resellers: number;
  status_counts: { status: string; count: number }[];
  sales_by_panel: DashboardPanelSales[];
  top_resellers: DashboardSalesRow[];
}

export const getDashboard = (period?: string) =>
  api.get("/api/reports/dashboard", { params: { period } })
    .then((r) => r.data as DashboardSummary);
export const getSalesByDay = (period?: string) =>
  api.get("/api/reports/sales-by-day", { params: { period } }).then((r) => r.data);
export const getSales = (params: any = {}) => api.get("/api/reports/sales", { params }).then((r) => r.data);
export const getDebts = () => api.get("/api/reports/debts").then((r) => r.data);
export const getZeroInvoices = (period?: string) =>
  api.get("/api/reports/zero-invoices", { params: { period } }).then((r) => r.data);
export const getDeliveryLog = (params: any = {}) =>
  api.get("/api/reports/delivery-log", { params }).then((r) => r.data);
export const getEnforcementActions = () =>
  api.get("/api/reports/enforcement-actions").then((r) => r.data);
export const getFinancialHistory = (params: any = {}) =>
  api.get("/api/reports/financial-history", { params }).then((r) => r.data);

// ---- operations ----
export interface BroadcastBody {
  text?: string; audience?: string; panel_id?: number; threshold?: number;
}
export const broadcastMessage = (body: BroadcastBody) =>
  api.post("/api/ops/broadcast", body).then((r) => r.data);
export const broadcastPreview = (body: BroadcastBody) =>
  api.post("/api/ops/broadcast/preview", body).then((r) => r.data);
export const broadcastStatus = () =>
  api.get("/api/ops/broadcast/status").then((r) => r.data);
export const panelMigrationPreview = (body: { panel_id: number; previous_host?: string }) =>
  api.post("/api/ops/broadcast/panel-migration/preview", body).then((r) => r.data);
export const panelMigration = (body: { panel_id: number; previous_host?: string }) =>
  api.post("/api/ops/broadcast/panel-migration", body).then((r) => r.data);
export const highVolumeUsers = (params: { threshold?: number; panel_id?: number; this_month_only?: boolean }) =>
  api.get("/api/reports/high-volume-users", { params }).then((r) => r.data);
export const highVolumeWarn = (body: { threshold?: number; panel_id?: number; this_month_only?: boolean; root_reseller_ids?: number[] }) =>
  api.post("/api/reports/high-volume-users/warn", body).then((r) => r.data);
export const runChannelGuard = () => api.post("/api/ops/channel-guard").then((r) => r.data);
export const setDomain = (domain: string, acme_email?: string) =>
  api.post("/api/ops/set-domain", { domain, acme_email }).then((r) => r.data);
export const restartService = () => api.post("/api/ops/restart").then((r) => r.data);
export const updateSystem = () => api.post("/api/ops/update").then((r) => r.data);
export const getUpdateStatus = () => api.get("/api/ops/update-status").then((r) => r.data);
// Lightweight liveness/version probe with a SHORT timeout, used while polling through an
// update (the backend bounces mid-rebuild). A short timeout keeps the poll loop responsive
// instead of hanging on the default 120s when a request stalls during the restart.
export const pingInfo = (timeoutMs = 4000) =>
  api.get("/api/info", { timeout: timeoutMs }).then((r) => r.data as { version: string });

// ---- account ----
export const updateAccount = (body: { current_password: string; new_username?: string; new_password?: string }) =>
  api.post("/api/auth/account", body).then((r) => r.data);

// ---- backup ----
export async function downloadBackup() {
  const res = await api.get("/api/ops/backup/download", { responseType: "blob" });
  const url = URL.createObjectURL(res.data as Blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "invoice-backup.zip";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
export const sendBackupToTelegram = () => api.post("/api/ops/backup/send").then((r) => r.data);
export const wipeData = () => api.post("/api/ops/wipe-data", { confirm: "DELETE" }).then((r) => r.data);
export const restoreBackup = (file: File, passphrase?: string) => {
  const fd = new FormData();
  fd.append("file", file);
  if (passphrase) fd.append("passphrase", passphrase);
  return api
    .post("/api/ops/backup/restore", fd, { headers: { "Content-Type": "multipart/form-data" } })
    .then((r) => r.data);
};

// ---- settings ----
export const listSettings = () => api.get("/api/settings").then((r) => r.data);
export const updateSettings = (items: { key: string; value: any }[]) =>
  api.patch("/api/settings", { items }).then((r) => r.data);
