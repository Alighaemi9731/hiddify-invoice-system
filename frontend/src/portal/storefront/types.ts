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

// ── wallet & top-up operations center (plan 005) ─────────────────────────────

export type TopupStatus = "pending" | "confirmed" | "rejected";
export type TopupMethod = "card" | "usdt" | "ton";

export interface TopupCustomerRef {
  id: number;
  telegram_id: number;
  name: string | null;
  username: string | null;
}

export interface TopupListItem {
  id: number;
  customer: TopupCustomerRef;
  // The current/credited value; `requested_amount_toman` preserves the customer's original request.
  amount_toman: number;
  requested_amount_toman: number | null;
  status: string;
  method: string | null;
  chain: string | null;
  txid: string | null;
  has_proof: boolean;
  created_at: string;
  decided_at: string | null;
}

export interface TopupDetail extends TopupListItem {
  note: string | null;
  credit_code_id: number | null;
}

export interface TopupListFilters {
  status?: TopupStatus;
  method?: TopupMethod;
  min_amount?: number;
  max_amount?: number;
  from?: string;
  to?: string;
  q?: string;
}

export interface TopupDecisionBody {
  decision: "confirm" | "reject";
  corrected_amount?: number;
  reason?: string;
}

export interface TopupDecisionResult {
  txn_id: number;
  decision: string;
  changed: boolean;
  already_decided: boolean;
  credited: number | null;
  requested: number | null;
  status: string | null;
}

export type BulkItemDecision = "confirm" | "reject";
export type BulkItemResult = "changed" | "already_decided" | "not_found" | "failed";

export interface BulkDecisionItem {
  txn_id: number;
  decision: BulkItemDecision;
}

export interface BulkDecisionBody {
  items: BulkDecisionItem[];
  reason?: string;
}

export interface BulkDecisionResult {
  results: Array<{ txn_id: number; result: BulkItemResult }>;
  counts: { changed: number; already_decided: number; not_found: number; failed: number };
}

export interface WalletAdjustmentBody {
  amount_toman_signed: number;
  reason: string;
}

export interface WalletAdjustmentResult {
  ledger_id: number;
  // The requested delta and the delta ACTUALLY applied can differ: a debit past zero is clamped so
  // the wallet floors at 0. The UI must show both distinctly when they diverge.
  requested_delta: number;
  applied_delta: number;
  old_balance: number;
  new_balance: number;
}

// ── credit codes (plan 006) ──────────────────────────────────────────────────
export type CreditKind = "percent" | "fixed";

export interface CreditCode {
  id: number;
  code: string;
  kind: CreditKind;
  percent_off: number | null;
  amount_toman: number | null;
  max_bonus_toman: number | null;
  min_topup_toman: number;
  is_gift: boolean;
  max_uses: number | null;
  per_customer_limit: number | null;
  used_count: number;
  enabled: boolean;
  archived: boolean;
  archived_at: string | null;
  starts_at: string | null;
  expires_at: string | null;
  created_at: string | null;
}

export interface CreditCodesPage {
  items: CreditCode[];
  next_cursor: string | null;
  config_version: number;
}

export interface CreditCreateBody {
  code: string;
  kind: CreditKind;
  percent_off?: number | null;
  amount_toman?: number | null;
  max_bonus_toman?: number | null;
  min_topup_toman?: number;
  is_gift?: boolean;
  max_uses?: number | null;
  per_customer_limit?: number | null;
  starts_at?: string | null;
  expires_at?: string | null;
}

export type CreditUpdateBody = Partial<Omit<CreditCreateBody, "code">> & { enabled?: boolean };

export interface CreditUsage {
  code: CreditCode;
  total_redemptions: number;
  unique_customers: number;
  total_bonus_toman: number;
  config_version: number;
}

export interface CreditRedemption {
  id: number;
  customer_id: number;
  wallet_txn_id: number | null;
  bonus_toman: number;
  created_at: string | null;
}

export interface CreditRedemptionsPage {
  items: CreditRedemption[];
  next_cursor: string | null;
  config_version: number;
}

// ── communications: broadcasts + direct messages (plan 006) ───────────────────
export type AudienceSegment = "all" | "expired" | "inactive30" | "trial_no_purchase";
export type BroadcastKind = "broadcast" | "direct";
export type BroadcastStatus = "queued" | "running" | "completed" | "canceled";

export interface AudiencePreview {
  segment: string;
  count: number;
  over_cap: boolean;
  sample: Array<{ id: number; name: string | null; username: string | null; telegram_id: number }>;
  config_version: number;
}

export interface BroadcastJob {
  id: number;
  kind: BroadcastKind;
  segment: string | null;
  status: BroadcastStatus;
  text: string;
  total: number;
  sent: number;
  blocked: number;
  failed: number;
  pending: number;
  created_at: string | null;
  canceled_at: string | null;
}

export interface BroadcastsPage {
  items: BroadcastJob[];
  next_cursor: string | null;
  config_version: number;
}

export interface BroadcastCreateResult {
  job_id: number;
  status: string;
  total: number;
}

export interface DirectMessageResult {
  delivery_id: number;
  status: string;
  total: number;
}
