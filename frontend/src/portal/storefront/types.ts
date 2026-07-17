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

// ── customer & order management (plan 004) ───────────────────────────────────

export type StorefrontOrderStatus =
  | "pending" | "provisioned" | "disabled" | "renewing" | "failed" | "deleted";

export type StorefrontLedgerKind =
  | "topup" | "purchase" | "manual_credit" | "manual_debit"
  | "refund" | "renew_reversal" | "credit_bonus";

export type StorefrontLedgerStatus = "pending" | "confirmed" | "rejected" | "done";

export type StorefrontActivityFilter = "active30" | "inactive30";

export interface CustomerListItem {
  id: number;
  telegram_id: number;
  name: string | null;
  username: string | null;
  banned: boolean;
  free_trial_used: boolean;
  wallet_balance_toman: number;
  last_seen_at: string | null;
  created_at: string;
  has_service: boolean;
}

export interface CustomerServiceCounts {
  total: number;
  active: number;
  by_status: Record<string, number>;
}

export interface CustomerDetail {
  id: number;
  telegram_id: number;
  name: string | null;
  username: string | null;
  banned: boolean;
  free_trial_used: boolean;
  wallet_balance_toman: number;
  net_ltv_toman: number;
  last_seen_at: string | null;
  created_at: string;
  service_counts: CustomerServiceCounts;
}

export interface LedgerRow {
  id: number;
  kind: StorefrontLedgerKind;
  amount_toman: number;
  status: StorefrontLedgerStatus;
  method: string | null;
  order_id: number | null;
  txid: string | null;
  chain: string | null;
  note: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface OrderSnapshot {
  used_gb: number;
  limit_gb: number;
  start_date: string | null;
  enabled: boolean;
  last_online: string | null;
}

export interface OrderCard {
  id: number;
  customer_id: number;
  label: string | null;
  status: StorefrontOrderStatus;
  is_trial: boolean;
  gb: number;
  days: number;
  price_toman: number;
  created_at: string;
  last_renewed_at: string | null;
  live_refreshed_at: string | null;
  snapshot: OrderSnapshot | null;
  freshness: "stored";
}

export interface OrderDetail extends OrderCard {
  customer: {
    id: number;
    telegram_id: number;
    name: string | null;
    username: string | null;
    banned: boolean;
  };
}

export interface CustomerListFilters {
  q?: string;
  banned?: boolean;
  activity?: StorefrontActivityFilter;
  has_service?: boolean;
}

export interface LedgerFilters {
  kind?: StorefrontLedgerKind;
  status?: StorefrontLedgerStatus;
  from?: string;
  to?: string;
}

export interface KeysetPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface CustomerStatusBody {
  banned: boolean;
  reason: string;
}

export interface OrderDeleteBody {
  confirm: "DELETE";
  reason: string;
}

export interface OrderLiveStatus {
  ok: boolean;
  used_gb?: number;
  limit_gb?: number;
  remaining_days?: number;
}

export interface OrderRefreshResult {
  status: OrderLiveStatus;
  freshness: "live" | "stale" | "unknown";
}

export interface OrderRenewResult {
  order: { id: number; renewed: boolean; gb: number; days: number };
}

export interface OrderOpResult {
  order: { id: number; status: string };
}

export interface CustomerBanResult {
  customer: { id: number; banned: boolean };
}
