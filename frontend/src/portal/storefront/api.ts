import { portalApi } from "../portalClient";
import type { StorefrontDashboard, StorefrontHealth, StorefrontShop } from "./types";

export const storefrontQueryKeys = {
  all: ["portal-storefronts"] as const,
  dashboard: (shopId: number, from: string, to: string) =>
    ["portal-storefronts", shopId, "dashboard", from, to] as const,
  health: (shopId: number) => ["portal-storefronts", shopId, "health"] as const,
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
