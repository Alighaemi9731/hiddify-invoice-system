import { portalApi } from "../portalClient";
import type {
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
  Versioned,
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
