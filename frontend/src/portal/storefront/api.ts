import { portalApi } from "../portalClient";
import type {
  BulkDecisionBody,
  BulkDecisionResult,
  CustomerBanResult,
  CustomerDetail,
  CustomerListFilters,
  CustomerListItem,
  CustomerStatusBody,
  KeysetPage,
  LedgerFilters,
  LedgerRow,
  OrderCard,
  OrderDetail,
  OrderOpResult,
  OrderRefreshResult,
  OrderRenewResult,
  StorefrontChannel,
  StorefrontCustomerPreview,
  StorefrontDashboard,
  StorefrontHealth,
  StorefrontManagers,
  StorefrontPlan,
  StorefrontPlanDraft,
  StorefrontPlanHistoryItem,
  StorefrontSettingsByGroup,
  StorefrontSettingsGroup,
  StorefrontSettings,
  StorefrontShop,
  TopupDecisionBody,
  TopupDecisionResult,
  TopupDetail,
  TopupListFilters,
  TopupListItem,
  Versioned,
  WalletAdjustmentBody,
  WalletAdjustmentResult,
} from "./types";

export const storefrontQueryKeys = {
  all: ["portal-storefronts"] as const,
  dashboard: (shopId: number, from: string, to: string) =>
    ["portal-storefronts", shopId, "dashboard", from, to] as const,
  health: (shopId: number) => ["portal-storefronts", shopId, "health"] as const,
  plans: (shopId: number) => ["portal-storefronts", shopId, "plans"] as const,
  planHistory: (shopId: number, planId: number) =>
    ["portal-storefronts", shopId, "plans", planId, "history"] as const,
  settings: (shopId: number) => ["portal-storefronts", shopId, "settings"] as const,
  managers: (shopId: number) => ["portal-storefronts", shopId, "managers"] as const,
  preview: (shopId: number) => ["portal-storefronts", shopId, "preview"] as const,
  customers: (shopId: number, filters: CustomerListFilters) =>
    ["portal-storefronts", shopId, "customers", filters] as const,
  customer: (shopId: number, customerId: number) =>
    ["portal-storefronts", shopId, "customer", customerId] as const,
  customerOrders: (shopId: number, customerId: number, status?: string) =>
    ["portal-storefronts", shopId, "customer", customerId, "orders", status ?? null] as const,
  customerLedger: (shopId: number, customerId: number, filters: LedgerFilters) =>
    ["portal-storefronts", shopId, "customer", customerId, "ledger", filters] as const,
  order: (shopId: number, orderId: number) =>
    ["portal-storefronts", shopId, "order", orderId] as const,
  topups: (shopId: number, filters: TopupListFilters) =>
    ["portal-storefronts", shopId, "topups", filters] as const,
  topup: (shopId: number, txnId: number) =>
    ["portal-storefronts", shopId, "topup", txnId] as const,
};

const etagOf = (headers: Record<string, unknown>, configVersion?: number) =>
  String(headers.etag || (configVersion ? `"sf-config-${configVersion}"` : ""));
const commandHeaders = (etag: string, idempotencyKey: string) => ({
  "If-Match": etag,
  "Idempotency-Key": idempotencyKey,
});
const resultOf = <T>(body: T | { result: T }): T => {
  if (body && typeof body === "object" && "result" in body) return (body as { result: T }).result;
  return body as T;
};
const mutationResult = <T>(response: { data: unknown; headers: Record<string, unknown> }) => {
  const body = response.data as T | { result: T; config_version?: number };
  const configVersion = body && typeof body === "object" && "config_version" in body
    ? body.config_version
    : undefined;
  return {
    data: resultOf<T>(body as T | { result: T }),
    etag: etagOf(response.headers, configVersion),
  } satisfies Versioned<T>;
};

export const listStorefronts = () =>
  portalApi.get("/api/portal/storefronts").then((response) => response.data as StorefrontShop[]);

export const getStorefrontDashboard = (shopId: number, from: string, to: string) =>
  portalApi
    .get(`/api/portal/storefronts/${shopId}/dashboard`, { params: { from, to } })
    .then((response) => response.data as StorefrontDashboard);

export const getStorefrontHealth = (shopId: number) =>
  portalApi
    .get(`/api/portal/storefronts/${shopId}/health`)
    .then((response) => response.data as StorefrontHealth);

export const listStorefrontPlans = (shopId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/plans`).then((response) => {
    const body = response.data as { items: StorefrontPlan[]; config_version: number };
    return {
      data: body.items,
      etag: etagOf(response.headers, body.config_version),
    } satisfies Versioned<StorefrontPlan[]>;
  });

export const createStorefrontPlan = (
  shopId: number,
  body: StorefrontPlanDraft,
  etag: string,
  idempotencyKey: string,
) => portalApi.post(`/api/portal/storefronts/${shopId}/plans`, body, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<StorefrontPlan>(response));

export const updateStorefrontPlan = (
  shopId: number,
  planId: number,
  body: Partial<StorefrontPlanDraft>,
  etag: string,
  idempotencyKey: string,
) => portalApi.patch(`/api/portal/storefronts/${shopId}/plans/${planId}`, body, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<StorefrontPlan>(response));

export const setStorefrontPlanEnabled = (
  shopId: number,
  planId: number,
  enabled: boolean,
  etag: string,
  idempotencyKey: string,
) => portalApi.put(`/api/portal/storefronts/${shopId}/plans/${planId}/enabled`, { enabled }, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<StorefrontPlan>(response));

export const deleteStorefrontPlan = (
  shopId: number,
  planId: number,
  etag: string,
  idempotencyKey: string,
) => portalApi.delete(`/api/portal/storefronts/${shopId}/plans/${planId}`, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<unknown>(response));

export const reorderStorefrontPlans = (
  shopId: number,
  planIds: number[],
  etag: string,
  idempotencyKey: string,
) => portalApi.put(`/api/portal/storefronts/${shopId}/plans/order`, { plan_ids: planIds }, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<StorefrontPlan[]>(response));

export const getStorefrontPlanHistory = (shopId: number, planId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/plans/${planId}/history`)
    .then((response) => {
      const body = response.data as StorefrontPlanHistoryItem[] | { items: StorefrontPlanHistoryItem[] };
      return Array.isArray(body) ? body : body.items;
    });

export const getStorefrontSettings = (shopId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/settings`).then((response) => {
    const body = response.data as StorefrontSettings;
    return {
      data: body,
      etag: etagOf(response.headers, body.config_version),
    } satisfies Versioned<StorefrontSettings>;
  });

export function updateStorefrontSettings<G extends StorefrontSettingsGroup>(
  shopId: number,
  group: G,
  body: Partial<StorefrontSettingsByGroup[G]>,
  etag: string,
  idempotencyKey: string,
) {
  return portalApi.patch(`/api/portal/storefronts/${shopId}/settings/${group}`, body, {
    headers: commandHeaders(etag, idempotencyKey),
  }).then((response) => mutationResult<StorefrontSettingsByGroup[G]>(response));
}

export const saveStorefrontChannel = (
  shopId: number,
  channelId: string,
  etag: string,
  idempotencyKey: string,
) => portalApi.post(`/api/portal/storefronts/${shopId}/channel`, { channel_id: channelId }, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<StorefrontChannel>(response));

export const setStorefrontChannelEnabled = (
  shopId: number,
  enabled: boolean,
  etag: string,
  idempotencyKey: string,
) => portalApi.put(`/api/portal/storefronts/${shopId}/channel/enabled`, { enabled }, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<StorefrontChannel>(response));

export const deleteStorefrontChannel = (
  shopId: number,
  etag: string,
  idempotencyKey: string,
) => portalApi.delete(`/api/portal/storefronts/${shopId}/channel`, {
  headers: commandHeaders(etag, idempotencyKey),
}).then((response) => mutationResult<unknown>(response));

export const listStorefrontManagers = (shopId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/managers`, {
    transformResponse: [parseManagerJson],
  }).then((response) => {
    const body = response.data as StorefrontManagers;
    return {
      data: body,
      etag: etagOf(response.headers, body.config_version),
    } satisfies Versioned<StorefrontManagers>;
  });

export const addStorefrontManager = (
  shopId: number,
  telegramId: string,
  etag: string,
  idempotencyKey: string,
) => portalApi.post(`/api/portal/storefronts/${shopId}/managers`, { telegram_id: telegramId }, {
  headers: commandHeaders(etag, idempotencyKey),
  transformResponse: [parseManagerJson],
}).then((response) => mutationResult<StorefrontManagers>(response));

export const removeStorefrontManager = (
  shopId: number,
  telegramId: string,
  etag: string,
  idempotencyKey: string,
) => portalApi.delete(`/api/portal/storefronts/${shopId}/managers/${encodeURIComponent(telegramId)}`, {
  headers: commandHeaders(etag, idempotencyKey),
  transformResponse: [parseManagerJson],
}).then((response) => mutationResult<StorefrontManagers>(response));

export const getStorefrontPreview = (shopId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/preview`)
    .then((response) => response.data as StorefrontCustomerPreview);

// ── customer & order management (plan 004) ───────────────────────────────────
// Entity mutations carry ONLY an Idempotency-Key (they are not shop-config edits, so no If-Match).
const entityHeaders = (idempotencyKey: string) => ({ "Idempotency-Key": idempotencyKey });

export const listCustomers = (
  shopId: number,
  filters: CustomerListFilters,
  cursor?: string | null,
  limit = 25,
) => portalApi.get(`/api/portal/storefronts/${shopId}/customers`, {
  params: {
    q: filters.q || undefined,
    banned: filters.banned,
    activity: filters.activity || undefined,
    has_service: filters.has_service,
    cursor: cursor || undefined,
    limit,
  },
}).then((response) => response.data as KeysetPage<CustomerListItem>);

export const getCustomer = (shopId: number, customerId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/customers/${customerId}`)
    .then((response) => response.data as CustomerDetail);

export const getCustomerLedger = (
  shopId: number,
  customerId: number,
  filters: LedgerFilters,
  cursor?: string | null,
  limit = 25,
) => portalApi.get(`/api/portal/storefronts/${shopId}/customers/${customerId}/ledger`, {
  params: {
    kind: filters.kind || undefined,
    status: filters.status || undefined,
    from: filters.from || undefined,
    to: filters.to || undefined,
    cursor: cursor || undefined,
    limit,
  },
}).then((response) => response.data as KeysetPage<LedgerRow>);

export const listCustomerOrders = (
  shopId: number,
  customerId: number,
  status?: string,
  cursor?: string | null,
  limit = 25,
) => portalApi.get(`/api/portal/storefronts/${shopId}/customers/${customerId}/orders`, {
  params: { status: status || undefined, cursor: cursor || undefined, limit },
}).then((response) => response.data as KeysetPage<OrderCard>);

export const getOrder = (shopId: number, orderId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/orders/${orderId}`)
    .then((response) => response.data as OrderDetail);

export const setCustomerStatus = (
  shopId: number,
  customerId: number,
  body: CustomerStatusBody,
  idempotencyKey: string,
) => portalApi.patch(`/api/portal/storefronts/${shopId}/customers/${customerId}/status`, body, {
  headers: entityHeaders(idempotencyKey),
}).then((response) => mutationResult<CustomerBanResult>(response));

// Live panel read. NOT wrapped in the {result} envelope; rate-limited (429 + Retry-After).
export const refreshOrder = (shopId: number, orderId: number, idempotencyKey: string) =>
  portalApi.post(`/api/portal/storefronts/${shopId}/orders/${orderId}/refresh`, undefined, {
    headers: entityHeaders(idempotencyKey),
  }).then((response) => response.data as OrderRefreshResult);

export const renewOrder = (shopId: number, orderId: number, idempotencyKey: string) =>
  portalApi.post(`/api/portal/storefronts/${shopId}/orders/${orderId}/renew`, undefined, {
    headers: entityHeaders(idempotencyKey),
  }).then((response) => mutationResult<OrderRenewResult>(response));

export const setOrderEnabled = (
  shopId: number,
  orderId: number,
  enabled: boolean,
  idempotencyKey: string,
) => portalApi.put(`/api/portal/storefronts/${shopId}/orders/${orderId}/enabled`, { enabled }, {
  headers: entityHeaders(idempotencyKey),
}).then((response) => mutationResult<OrderOpResult>(response));

export const deleteOrder = (
  shopId: number,
  orderId: number,
  reason: string,
  idempotencyKey: string,
) => portalApi.delete(`/api/portal/storefronts/${shopId}/orders/${orderId}`, {
  data: { confirm: "DELETE", reason },
  headers: entityHeaders(idempotencyKey),
}).then((response) => mutationResult<OrderOpResult>(response));

// ── wallet & top-up operations center (plan 005) ─────────────────────────────
// Reads are plain lists; mutations carry ONLY an Idempotency-Key (entity edits, no If-Match).

export const listTopups = (
  shopId: number,
  filters: TopupListFilters,
  cursor?: string | null,
  limit = 25,
) => portalApi.get(`/api/portal/storefronts/${shopId}/topups`, {
  params: {
    status: filters.status || undefined,
    method: filters.method || undefined,
    min_amount: filters.min_amount ?? undefined,
    max_amount: filters.max_amount ?? undefined,
    from: filters.from || undefined,
    to: filters.to || undefined,
    q: filters.q || undefined,
    cursor: cursor || undefined,
    limit,
  },
}).then((response) => response.data as KeysetPage<TopupListItem>);

export const getTopup = (shopId: number, txnId: number) =>
  portalApi.get(`/api/portal/storefronts/${shopId}/topups/${txnId}`)
    .then((response) => response.data as TopupDetail);

// The raw GET path for the proof stream. Because the portal authenticates with a bearer header
// (not a cookie), a plain <img src> would 401 — the proof is fetched as an authenticated blob
// (getTopupProof) and shown via an object URL, mirroring openPortalPaymentProof.
export const topupProofUrl = (shopId: number, txnId: number) =>
  `/api/portal/storefronts/${shopId}/topups/${txnId}/proof`;

// Fetch the proof as an ArrayBuffer, NOT responseType:"blob". Axios's blob response transform
// calls `.stream()` on the mocked/undici Response, which throws under Node 22 (CI). We wrap the raw
// bytes in a Blob ourselves; a 404 still rejects as a normal axios error, so isNotFound() holds.
export const getTopupProof = (shopId: number, txnId: number) =>
  portalApi.get(topupProofUrl(shopId, txnId), { responseType: "arraybuffer" })
    .then((response) => new Blob([response.data as ArrayBuffer], {
      type: (response.headers["content-type"] as string | undefined) || "image/jpeg",
    }));

export const decideTopup = (
  shopId: number,
  txnId: number,
  body: TopupDecisionBody,
  idempotencyKey: string,
) => portalApi.post(`/api/portal/storefronts/${shopId}/topups/${txnId}/decision`, body, {
  headers: entityHeaders(idempotencyKey),
}).then((response) => mutationResult<TopupDecisionResult>(response));

export const bulkDecideTopups = (
  shopId: number,
  body: BulkDecisionBody,
  idempotencyKey: string,
) => portalApi.post(`/api/portal/storefronts/${shopId}/topups/bulk-decisions`, body, {
  headers: entityHeaders(idempotencyKey),
}).then((response) => mutationResult<BulkDecisionResult>(response));

export const adjustWallet = (
  shopId: number,
  customerId: number,
  body: WalletAdjustmentBody,
  idempotencyKey: string,
) => portalApi.post(
  `/api/portal/storefronts/${shopId}/customers/${customerId}/wallet-adjustments`, body, {
    headers: entityHeaders(idempotencyKey),
  },
).then((response) => mutationResult<WalletAdjustmentResult>(response));

function parseManagerJson(data: unknown) {
  if (typeof data !== "string") return data;
  const losslessIds = data.replace(
    /("(?:owner_id|telegram_id)"\s*:\s*)([0-9]+)/g,
    '$1"$2"',
  );
  return JSON.parse(losslessIds);
}

export function currentTehranMonthRange(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tehran",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const value = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((part) => part.type === type)?.value);
  const year = value("year");
  const month = value("month");
  const day = value("day");
  const from = `${year}-${String(month).padStart(2, "0")}-01`;
  const to = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  return { from, to };
}
