export type StorefrontHealthErrorClass =
  | "unauthorized"
  | "network"
  | "configuration"
  | "unknown"
  | null;

export type StorefrontServiceState =
  | "pending"
  | "renewing"
  | "provisioned"
  | "disabled"
  | "failed"
  | "deleted";

export type StorefrontOperationState =
  | "pending"
  | "in_progress"
  | "done"
  | "failed"
  | "reversed";

export interface StorefrontShop {
  id: number;
  reseller: { id: number; name: string };
  panel: { id: number; key: string };
  bot_username: string | null;
  enabled: boolean;
  status: string;
  health_error_class: StorefrontHealthErrorClass;
  health_state_updated_at: string | null;
  shop_closed: boolean;
  role: "owner";
}

export interface StorefrontSalesBucket {
  count: number;
  amount_toman: number;
}

export interface StorefrontSalesPeriod {
  gross_sales_toman: number;
  reversals_toman: number;
  net_sales_toman: number;
  purchase: StorefrontSalesBucket;
  renewal: StorefrontSalesBucket;
  unknown: StorefrontSalesBucket;
}

export interface StorefrontDashboard {
  storefront_id: number;
  range: { from_date: string; to_date: string; timezone: "Asia/Tehran" };
  sales_today: StorefrontSalesPeriod;
  sales_month: StorefrontSalesPeriod;
  sales_range: StorefrontSalesPeriod;
  customers: {
    total: number;
    active_30d: number;
    wallet_liability_toman: number;
  };
  service_states: Record<StorefrontServiceState, number>;
  near_expiry: number;
  pending_topups: { count: number; amount_toman: number };
  credits: { redemptions: number; bonus_toman: number };
  operation_states: Record<StorefrontOperationState, number>;
  trial_conversion: {
    trial_customers: number;
    converted_customers: number;
    rate: number | null;
  };
}

export interface StorefrontHealth {
  storefront_id: number;
  bot: {
    enabled: boolean;
    status: string;
    error_class: StorefrontHealthErrorClass;
    state_updated_at: string | null;
  };
  panel: {
    id: number;
    key: string;
    enabled: boolean;
    status: string;
    last_synced_at: string | null;
    error_class: StorefrontHealthErrorClass;
    state_updated_at: string | null;
  };
  operation_states: Record<StorefrontOperationState, number>;
}
