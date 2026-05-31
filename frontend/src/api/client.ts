import axios from "axios";

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
      if (!location.pathname.startsWith("/login")) location.href = "/login";
    }
    return Promise.reject(err);
  }
);

export const pdfUrl = (invoiceId: number) =>
  `${baseURL}/api/invoices/${invoiceId}/pdf`;

// Fetch the PDF WITH the auth header (a plain link would 401), then open it.
export async function openInvoicePdf(invoiceId: number) {
  const res = await api.get(`/api/invoices/${invoiceId}/pdf`, { responseType: "blob" });
  const url = URL.createObjectURL(res.data as Blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

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
export const listResellers = (params: any = {}) =>
  api.get("/api/resellers", { params }).then((r) => r.data);
export const getResellerTree = (params: any = {}) =>
  api.get("/api/resellers/tree", { params }).then((r) => r.data);
export const updateReseller = (id: number, b: any) =>
  api.patch(`/api/resellers/${id}`, b).then((r) => r.data);
export const enforceReseller = (id: number, dry_run?: boolean) =>
  api.post(`/api/resellers/${id}/enforce`, null, { params: { dry_run } }).then((r) => r.data);
export const restoreReseller = (id: number) =>
  api.post(`/api/resellers/${id}/restore`).then((r) => r.data);

// ---- invoices ----
export const listInvoices = (params: any = {}) =>
  api.get("/api/invoices", { params }).then((r) => r.data);
export const getInvoice = (id: number) => api.get(`/api/invoices/${id}`).then((r) => r.data);
export const generateInvoices = (b: any) => api.post("/api/invoices/generate", b).then((r) => r.data);
export const sendInvoice = (id: number) => api.post(`/api/invoices/${id}/send`).then((r) => r.data);
export const sendPeriod = (period: string) =>
  api.post("/api/invoices/send-period", null, { params: { period } }).then((r) => r.data);
export const markInvoicePaid = (id: number) => api.post(`/api/invoices/${id}/mark-paid`).then((r) => r.data);
export const unmarkInvoicePaid = (id: number) => api.post(`/api/invoices/${id}/unmark-paid`).then((r) => r.data);
export const editInvoice = (id: number, body: any) => api.patch(`/api/invoices/${id}`, body).then((r) => r.data);
export const cancelInvoice = (id: number) => api.post(`/api/invoices/${id}/cancel`).then((r) => r.data);
export const deferInvoice = (id: number, body: { deferred_until: string | null; defer_note?: string }) =>
  api.post(`/api/invoices/${id}/defer`, body).then((r) => r.data);

// ---- payments ----
export const listPayments = (params: any = {}) =>
  api.get("/api/payments", { params }).then((r) => r.data);
export const verifyPayment = (id: number) => api.post(`/api/payments/${id}/verify`).then((r) => r.data);
export const confirmPayment = (id: number) => api.post(`/api/payments/${id}/confirm`).then((r) => r.data);
export const rejectPayment = (id: number) => api.post(`/api/payments/${id}/reject`).then((r) => r.data);

// ---- reports ----
export const getDashboard = (period?: string) =>
  api.get("/api/reports/dashboard", { params: { period } }).then((r) => r.data);
export const getSales = (params: any = {}) => api.get("/api/reports/sales", { params }).then((r) => r.data);
export const getDebts = () => api.get("/api/reports/debts").then((r) => r.data);
export const getZeroInvoices = (period?: string) =>
  api.get("/api/reports/zero-invoices", { params: { period } }).then((r) => r.data);
export const getDeliveryLog = (params: any = {}) =>
  api.get("/api/reports/delivery-log", { params }).then((r) => r.data);
export const getEnforcementActions = () =>
  api.get("/api/reports/enforcement-actions").then((r) => r.data);

// ---- operations ----
export const runDunning = () => api.post("/api/ops/dunning/run").then((r) => r.data);
export const runMonthly = (params: any = {}) =>
  api.post("/api/ops/run-monthly", null, { params }).then((r) => r.data);
export const broadcastMessage = (body: { text: string; audience?: string; panel_id?: number }) =>
  api.post("/api/ops/broadcast", body).then((r) => r.data);
export const runChannelGuard = () => api.post("/api/ops/channel-guard").then((r) => r.data);
export const setDomain = (domain: string, acme_email?: string) =>
  api.post("/api/ops/set-domain", { domain, acme_email }).then((r) => r.data);

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
export const restoreBackup = (file: File) => {
  const fd = new FormData();
  fd.append("file", file);
  return api
    .post("/api/ops/backup/restore", fd, { headers: { "Content-Type": "multipart/form-data" } })
    .then((r) => r.data);
};

// ---- settings ----
export const listSettings = () => api.get("/api/settings").then((r) => r.data);
export const updateSettings = (items: { key: string; value: any }[]) =>
  api.patch("/api/settings", { items }).then((r) => r.data);
