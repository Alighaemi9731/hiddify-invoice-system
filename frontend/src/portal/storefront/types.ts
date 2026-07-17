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

export interface Versioned<T> {
  data: T;
  etag: string;
}

export interface StorefrontPlan {
  id: number;
  title: string;
  gb: number;
  days: number;
  price_toman: number;
  enabled: boolean;
  sort_order: number;
}

export interface StorefrontPlanDraft {
  title?: string;
  gb: number;
  days: number;
  price_toman: number;
}

export interface StorefrontPlanHistoryItem {
  id: number;
  action: string;
  actor_telegram_id: string | null;
  actor_role: string;
  source: string;
  before: Partial<StorefrontPlan> | null;
  after: Partial<StorefrontPlan> | null;
  outcome: string;
  created_at: string;
}

export interface StorefrontPaymentSettings {
  pay_card_enabled: boolean;
  pay_usdt_enabled: boolean;
  pay_ton_enabled: boolean;
  card_number: string | null;
  card_holder: string | null;
  usdt_address: string | null;
  ton_address: string | null;
}

export interface StorefrontTrialSettings {
  free_trial_enabled: boolean;
  free_trial_gb: number;
  free_trial_days: number;
}

export interface StorefrontMessageSettings {
  welcome_text: string | null;
  support_contact: string | null;
}

export interface StorefrontShopStateSettings {
  shop_closed: boolean;
  closed_text: string | null;
}

export type StorefrontSettingsGroup = "payment" | "trial" | "messages" | "shop-state";
export type StorefrontSettingsByGroup = {
  payment: StorefrontPaymentSettings;
  trial: StorefrontTrialSettings;
  messages: StorefrontMessageSettings;
  "shop-state": StorefrontShopStateSettings;
};

export interface StorefrontChannel {
  channel_id: string | null;
  channel_link: string | null;
  channel_required: boolean;
  verified: boolean;
  health: "ok" | "disabled" | "error";
}

export interface StorefrontSettings {
  payment: StorefrontPaymentSettings;
  trial: StorefrontTrialSettings;
  messages: StorefrontMessageSettings;
  shop_state: StorefrontShopStateSettings;
  channel: StorefrontChannel;
  config_version: number;
}

export interface StorefrontManager {
  telegram_id: string;
  role: "owner" | "manager";
  removable: boolean;
}

export interface StorefrontManagers {
  owner_id: string;
  items: StorefrontManager[];
  max_count: number;
  config_version: number;
}

export interface StorefrontCustomerPreview {
  welcome_text: string;
  support_contact: string | null;
  shop_closed: boolean;
  closed_text: string | null;
  free_trial: {
    enabled: boolean;
    gb: number;
    days: number;
  };
  payment_methods: string[];
  enabled_plans: Array<Pick<StorefrontPlan, "id" | "title" | "gb" | "days" | "price_toman">>;
  channel_required: boolean;
}
