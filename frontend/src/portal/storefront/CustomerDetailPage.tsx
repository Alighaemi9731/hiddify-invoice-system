import { useMemo, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent,
  DialogContentText, DialogTitle, Divider, Grid, MenuItem, Stack, TextField, Typography,
} from "@mui/material";
import ArrowForwardIcon from "@mui/icons-material/esm/ArrowForward";
import AutorenewIcon from "@mui/icons-material/esm/Autorenew";
import BlockIcon from "@mui/icons-material/esm/Block";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import LockOpenIcon from "@mui/icons-material/esm/LockOpen";
import MailOutlineIcon from "@mui/icons-material/esm/MailOutline";
import PauseCircleOutlineIcon from "@mui/icons-material/esm/PauseCircleOutline";
import PlayCircleOutlineIcon from "@mui/icons-material/esm/PlayCircleOutline";
import RefreshIcon from "@mui/icons-material/esm/Refresh";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useOutletContext, useParams } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { NumberField } from "../../components/NumberField";
import { fmtDateTime, fmtGb, fmtNum, fmtToman } from "../../format";
import { SectionCard } from "../ui";
import {
  adjustWallet, deleteOrder, getCustomer, getCustomerLedger, listCustomerOrders, refreshOrder,
  renewOrder, sendDirectMessage, setCustomerStatus, setOrderEnabled, storefrontQueryKeys,
} from "./api";
import StorefrontConflictDialog from "./StorefrontConflictDialog";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type {
  DirectMessageResult, LedgerFilters, LedgerRow, OrderCard, OrderRefreshResult, OrderRenewResult,
  Versioned, WalletAdjustmentBody, WalletAdjustmentResult,
} from "./types";
import {
  commandRecoveryMessage, isNotFound, isVersionConflict, rateLimitRetryAfter,
  storefrontErrorMessage, useIdempotentMutation,
} from "./mutation";
import { useXsFullScreen } from "../../responsive";

const ORDER_STATUS_FA: Record<string, string> = {
  pending: "در انتظار",
  provisioned: "فعال",
  disabled: "غیرفعال",
  renewing: "در حال تمدید",
  failed: "ناموفق",
  deleted: "حذف‌شده",
};
const orderStatusColor = (status: string): "success" | "error" | "warning" | "default" =>
  status === "provisioned" ? "success"
    : status === "failed" ? "error"
      : status === "disabled" ? "warning" : "default";

const LEDGER_KIND_FA: Record<string, string> = {
  topup: "شارژ کیف پول",
  purchase: "خرید",
  manual_credit: "بستانکاری دستی",
  manual_debit: "بدهکاری دستی",
  refund: "بازگشت وجه",
  renew_reversal: "برگشت تمدید",
  credit_bonus: "پاداش اعتبار",
};
const LEDGER_STATUS_FA: Record<string, string> = {
  pending: "در انتظار",
  confirmed: "تأییدشده",
  rejected: "ردشده",
  done: "انجام‌شده",
};
const FRESHNESS_FA: Record<string, string> = { live: "زنده", stale: "قدیمی", unknown: "نامشخص" };

type EntityCommand =
  | { type: "ban"; banned: boolean; reason: string }
  | { type: "renew"; orderId: number }
  | { type: "enabled"; orderId: number; enabled: boolean }
  | { type: "delete"; orderId: number; reason: string };

type Confirm =
  | { kind: "ban"; banned: boolean }
  | { kind: "renew"; orderId: number }
  | { kind: "enabled"; orderId: number; enabled: boolean }
  | { kind: "delete"; orderId: number };

type LiveEntry = { result?: OrderRefreshResult; retryAfter?: number; error?: boolean };

export default function CustomerDetailPage() {
  const xsFull = useXsFullScreen();
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const params = useParams();
  const customerId = Number(params.customerId);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const customerQuery = useQuery({
    queryKey: storefrontQueryKeys.customer(shop.id, customerId),
    queryFn: () => getCustomer(shop.id, customerId),
    enabled: Number.isSafeInteger(customerId) && customerId > 0,
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const ordersQuery = useInfiniteQuery({
    queryKey: storefrontQueryKeys.customerOrders(shop.id, customerId),
    queryFn: ({ pageParam }) => listCustomerOrders(shop.id, customerId, undefined, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: !!customerQuery.data,
  });
  const orders = ordersQuery.data?.pages.flatMap((page) => page.items) ?? [];

  const [live, setLive] = useState<Record<number, LiveEntry>>({});
  const [confirmAction, setConfirmAction] = useState<Confirm | null>(null);
  const [reason, setReason] = useState("");
  const [deleteText, setDeleteText] = useState("");
  const [conflict, setConflict] = useState<EntityCommand | null>(null);
  const [conflictReloadError, setConflictReloadError] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const invalidateAfterCommand = (orderId?: number) => Promise.all([
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "customer", customerId] }),
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "customers"] }),
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "dashboard"] }),
    orderId
      ? queryClient.invalidateQueries({ queryKey: storefrontQueryKeys.order(shop.id, orderId) })
      : Promise.resolve(),
  ]);

  const command = useIdempotentMutation<Versioned<unknown>, EntityCommand>(
    async (input, key) => {
      if (input.type === "ban") {
        return setCustomerStatus(shop.id, customerId, { banned: input.banned, reason: input.reason }, key);
      }
      if (input.type === "renew") return renewOrder(shop.id, input.orderId, key);
      if (input.type === "enabled") return setOrderEnabled(shop.id, input.orderId, input.enabled, key);
      return deleteOrder(shop.id, input.orderId, input.reason, key);
    },
    {
      // One lane per order (plus a separate one for the customer-level ban): renewing order A must
      // not block deleting order B just because A's command — a real panel round-trip, not a fast
      // DB write — is still in flight.
      commandKey: (input) => (input.type === "ban" ? "ban" : `${input.type}:${input.orderId}`),
      onSuccess: async (result, variables) => {
        await invalidateAfterCommand("orderId" in variables ? variables.orderId : undefined);
        setMessage(successMessage(variables, result));
        setConfirmAction(null);
        setReason("");
        setDeleteText("");
      },
      onError: (error, variables) => {
        if (isVersionConflict(error)) setConflict(variables);
      },
    },
  );

  const orderBusy = (orderId: number) =>
    command.isPending && command.variables !== undefined
    && "orderId" in command.variables && command.variables.orderId === orderId;

  const refresh = useIdempotentMutation<OrderRefreshResult, { orderId: number }>(
    (input, key) => refreshOrder(shop.id, input.orderId, key),
    {
      commandKey: (input) => String(input.orderId),
      onSuccess: (result, variables) =>
        setLive((prev) => ({ ...prev, [variables.orderId]: { result } })),
      onError: (error, variables) => {
        const retryAfter = rateLimitRetryAfter(error);
        setLive((prev) => ({
          ...prev,
          [variables.orderId]: retryAfter != null ? { retryAfter } : { error: true },
        }));
      },
    },
  );

  const [adjAmount, setAdjAmount] = useState("");
  const [adjReason, setAdjReason] = useState("");
  const [adjResult, setAdjResult] = useState<WalletAdjustmentResult | null>(null);
  const [msgOpen, setMsgOpen] = useState(false);

  const walletMutation = useIdempotentMutation<Versioned<WalletAdjustmentResult>, WalletAdjustmentBody>(
    (input, key) => adjustWallet(shop.id, customerId, input, key),
    {
      onSuccess: async (result) => {
        await invalidateAfterCommand();
        setAdjResult(result.data);
        setAdjAmount("");
        setAdjReason("");
      },
    },
  );

  const adjNum = Number(adjAmount);
  const adjAmountValid = adjAmount.trim() !== "" && Number.isFinite(adjNum) && adjNum !== 0
    && adjNum >= -(10 ** 12) && adjNum <= 10 ** 12;
  const adjReasonValid = adjReason.trim().length >= 3 && adjReason.trim().length <= 255;
  const adjValid = adjAmountValid && adjReasonValid;
  const submitAdjustment = () => {
    if (!adjValid || walletMutation.isPending) return;
    walletMutation.mutate({ amount_toman_signed: Math.round(adjNum), reason: adjReason.trim() });
  };
  const walletError = walletMutation.isError
    ? commandRecoveryMessage(walletMutation.error)
      || (isNotFound(walletMutation.error)
        ? "این مشتری دیگر در دسترس نیست؛ صفحه را تازه‌سازی کنید."
        : "ثبت تغییر موجودی ناموفق بود. مبلغ و دلیل را بررسی کنید.")
    : null;

  const commandError = command.isError && !isVersionConflict(command.error)
    ? (isNotFound(command.error)
      ? "این مورد دیگر در دسترس نیست؛ صفحه را تازه‌سازی کنید."
      : storefrontErrorMessage(command.error, "انجام عملیات ناموفق بود؛ دوباره تلاش کنید."))
    : null;

  const openConfirm = (action: Confirm) => {
    setReason("");
    setDeleteText("");
    setConfirmAction(action);
  };

  const confirmValid = !confirmAction ? false
    : confirmAction.kind === "ban"
      ? reason.trim().length >= 3 && reason.trim().length <= 255
      : confirmAction.kind === "delete"
        ? deleteText === "DELETE" && reason.trim().length >= 3 && reason.trim().length <= 255
        : true;

  const submitConfirm = () => {
    if (!confirmAction || !confirmValid || command.isPending) return;
    if (confirmAction.kind === "ban") {
      command.mutate({ type: "ban", banned: confirmAction.banned, reason: reason.trim() });
    } else if (confirmAction.kind === "renew") {
      command.mutate({ type: "renew", orderId: confirmAction.orderId });
    } else if (confirmAction.kind === "enabled") {
      command.mutate({ type: "enabled", orderId: confirmAction.orderId, enabled: confirmAction.enabled });
    } else {
      command.mutate({ type: "delete", orderId: confirmAction.orderId, reason: reason.trim() });
    }
  };

  if (customerQuery.isError && isNotFound(customerQuery.error)) {
    return (
      <Box>
        <BackButton onBack={() => navigate(`/portal/storefront/${shop.id}/customers`)} />
        <Alert severity="warning">این مشتری پیدا نشد یا به فروشگاه شما تعلق ندارد.</Alert>
      </Box>
    );
  }

  const customer = customerQuery.data;

  return (
    <Box>
      <BackButton onBack={() => navigate(`/portal/storefront/${shop.id}/customers`)} />
      {message && <Alert severity="success" onClose={() => setMessage(null)} sx={{ mb: 2 }}>{message}</Alert>}
      {commandError && <Alert severity="error" sx={{ mb: 2 }}>{commandError}</Alert>}

      <DataState
        isLoading={customerQuery.isLoading}
        isError={customerQuery.isError}
        rows={6}
        onRetry={() => customerQuery.refetch()}
      >
        {customer && (
          <Grid container spacing={2}>
            <Grid item xs={12} md={6}>
              <SectionCard title="هویت مشتری">
                <Stack spacing={1}>
                  <Typography variant="h6" sx={{ fontWeight: 800 }}>
                    {customer.name?.trim() || "بدون نام"}
                  </Typography>
                  <InfoRow label="شناسهٔ تلگرام" value={`#${fmtNum(customer.telegram_id)}`} ltr />
                  <InfoRow
                    label="نام کاربری"
                    value={customer.username ? `@${customer.username.replace(/^@/, "")}` : "—"}
                    ltr
                  />
                  <InfoRow label="تاریخ عضویت" value={fmtDateTime(customer.created_at)} />
                  <InfoRow label="آخرین فعالیت" value={fmtDateTime(customer.last_seen_at)} />
                </Stack>
              </SectionCard>
            </Grid>

            <Grid item xs={12} md={6}>
              <SectionCard title="وضعیت و دسترسی">
                <Stack spacing={1.5}>
                  <Stack direction="row" spacing={1} flexWrap="wrap">
                    <Chip
                      color={customer.banned ? "error" : "success"}
                      variant="outlined"
                      icon={customer.banned ? <BlockIcon /> : <LockOpenIcon />}
                      label={customer.banned ? "مسدود" : "فعال"}
                    />
                    <Chip
                      variant="outlined"
                      label={customer.free_trial_used ? "تست رایگان استفاده‌شده" : "تست رایگان استفاده‌نشده"}
                    />
                    <Chip
                      variant="outlined"
                      label={`سرویس‌ها: ${fmtNum(customer.service_counts.active)} فعال از ${fmtNum(customer.service_counts.total)}`}
                    />
                  </Stack>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ alignSelf: "flex-start" }}>
                    <Button
                      variant="outlined"
                      color={customer.banned ? "success" : "error"}
                      startIcon={customer.banned ? <LockOpenIcon /> : <BlockIcon />}
                      disabled={command.isPending}
                      onClick={() => openConfirm({ kind: "ban", banned: !customer.banned })}
                    >
                      {customer.banned ? "رفع مسدودی" : "مسدودسازی مشتری"}
                    </Button>
                    <Button
                      variant="outlined"
                      startIcon={<MailOutlineIcon />}
                      disabled={customer.banned}
                      onClick={() => setMsgOpen(true)}
                    >
                      ارسال پیام
                    </Button>
                  </Stack>
                </Stack>
              </SectionCard>
            </Grid>

            <Grid item xs={12}>
              <SectionCard title="کیف پول">
                <Stack direction={{ xs: "column", sm: "row" }} spacing={3}>
                  <Box>
                    <Typography variant="caption" color="text.secondary">موجودی کیف پول</Typography>
                    <Typography variant="h6" sx={{ fontWeight: 850 }}>
                      {fmtToman(customer.wallet_balance_toman)}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">ارزش خالص طول عمر (LTV)</Typography>
                    <Typography variant="h6" sx={{ fontWeight: 850 }}>
                      {fmtToman(customer.net_ltv_toman)}
                    </Typography>
                  </Box>
                </Stack>

                <Divider sx={{ my: 2 }} />

                <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 1 }}>تنظیم دستی موجودی</Typography>
                <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "flex-start" }}>
                  <NumberField
                    size="small" label="مبلغ (تومان)" value={adjAmount}
                    onChange={setAdjAmount}
                    allowNegative
                    error={adjAmount.trim() !== "" && !adjAmountValid}
                    helperText="مثبت برای افزایش، منفی برای کاهش"
                    disabled={walletMutation.isPending}
                    sx={{ minWidth: { xs: "100%", sm: 180 } }}
                  />
                  <TextField
                    size="small" fullWidth label="دلیل (الزامی)" value={adjReason}
                    onChange={(event) => setAdjReason(event.target.value)}
                    error={!!adjReason && !adjReasonValid}
                    helperText="بین ۳ تا ۲۵۵ نویسه"
                    inputProps={{ maxLength: 255 }}
                    disabled={walletMutation.isPending}
                  />
                  <Button
                    variant="contained"
                    disabled={!adjValid || walletMutation.isPending}
                    onClick={submitAdjustment}
                    sx={{ whiteSpace: "nowrap", alignSelf: { xs: "stretch", sm: "flex-start" }, mt: { sm: 0.25 } }}
                  >
                    ثبت تغییر موجودی
                  </Button>
                </Stack>

                {walletError && <Alert severity="error" sx={{ mt: 1.5 }}>{walletError}</Alert>}
                {adjResult && (
                  <Alert
                    severity={adjResult.requested_delta !== adjResult.applied_delta ? "warning" : "success"}
                    onClose={() => setAdjResult(null)}
                    sx={{ mt: 1.5 }}
                  >
                    <Typography variant="body2">
                      موجودی از {fmtToman(adjResult.old_balance)} به {fmtToman(adjResult.new_balance)} تغییر کرد.
                    </Typography>
                    <Typography variant="body2">
                      تغییر درخواستی: {fmtSignedToman(adjResult.requested_delta)}
                      {adjResult.requested_delta !== adjResult.applied_delta
                        ? ` · تغییر اعمال‌شده: ${fmtSignedToman(adjResult.applied_delta)}`
                        : ""}
                    </Typography>
                    {adjResult.requested_delta !== adjResult.applied_delta && (
                      <Typography variant="caption" sx={{ display: "block", mt: 0.5 }}>
                        موجودی نمی‌تواند منفی شود؛ کسر تا صفر اعمال شد.
                      </Typography>
                    )}
                  </Alert>
                )}
              </SectionCard>
            </Grid>

            <Grid item xs={12}>
              <Typography variant="subtitle1" sx={{ fontWeight: 800, mb: 1 }}>سرویس‌ها</Typography>
              <DataState
                isLoading={ordersQuery.isLoading}
                isError={ordersQuery.isError}
                rows={3}
                onRetry={() => ordersQuery.refetch()}
              >
                {!orders.length ? (
                  <Alert severity="info">این مشتری هنوز سرویسی ندارد.</Alert>
                ) : (
                  <Stack spacing={1.5}>
                    {orders.map((order) => (
                      <OrderCardView
                        key={order.id}
                        order={order}
                        live={live[order.id]}
                        busy={orderBusy(order.id)}
                        refreshing={refresh.isPending && refresh.variables?.orderId === order.id}
                        refreshDisabled={refresh.isPending && refresh.variables?.orderId === order.id}
                        onRefresh={() => refresh.mutate({ orderId: order.id })}
                        onRenew={() => openConfirm({ kind: "renew", orderId: order.id })}
                        onToggle={(enabled) => openConfirm({ kind: "enabled", orderId: order.id, enabled })}
                        onDelete={() => openConfirm({ kind: "delete", orderId: order.id })}
                      />
                    ))}
                    {ordersQuery.hasNextPage && (
                      <Button
                        variant="outlined"
                        onClick={() => ordersQuery.fetchNextPage()}
                        disabled={ordersQuery.isFetchingNextPage}
                        sx={{ alignSelf: "center" }}
                      >
                        {ordersQuery.isFetchingNextPage ? "در حال بارگذاری…" : "نمایش سرویس‌های بیشتر"}
                      </Button>
                    )}
                  </Stack>
                )}
              </DataState>
            </Grid>

            <Grid item xs={12}>
              <LedgerSection shopId={shop.id} customerId={customerId} />
            </Grid>
          </Grid>
        )}
      </DataState>

      <Dialog
        open={!!confirmAction}
        onClose={() => !command.isPending && setConfirmAction(null)}
        maxWidth="xs"
        fullWidth
        fullScreen={xsFull}
      >
        <DialogTitle>{confirmTitle(confirmAction)}</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: confirmAction && confirmAction.kind !== "renew" && confirmAction.kind !== "enabled" ? 2 : 0 }}>
            {confirmBody(confirmAction)}
          </DialogContentText>
          {confirmAction?.kind === "ban" && (
            <TextField
              autoFocus fullWidth label="دلیل (الزامی)" value={reason}
              error={!!reason && (reason.trim().length < 3 || reason.trim().length > 255)}
              helperText="بین ۳ تا ۲۵۵ نویسه"
              inputProps={{ maxLength: 255 }}
              disabled={command.isPending}
              onChange={(event) => setReason(event.target.value)}
            />
          )}
          {confirmAction?.kind === "delete" && (
            <Stack spacing={2}>
              <TextField
                autoFocus fullWidth label="برای تأیید، عبارت DELETE را وارد کنید"
                value={deleteText}
                error={!!deleteText && deleteText !== "DELETE"}
                inputProps={{ dir: "ltr" }}
                disabled={command.isPending}
                onChange={(event) => setDeleteText(event.target.value)}
              />
              <TextField
                fullWidth label="دلیل (الزامی)" value={reason}
                error={!!reason && (reason.trim().length < 3 || reason.trim().length > 255)}
                helperText="بین ۳ تا ۲۵۵ نویسه"
                inputProps={{ maxLength: 255 }}
                disabled={command.isPending}
                onChange={(event) => setReason(event.target.value)}
              />
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmAction(null)} disabled={command.isPending}>انصراف</Button>
          <Button
            variant="contained"
            color={confirmAction?.kind === "delete" || (confirmAction?.kind === "ban" && confirmAction.banned) ? "error" : "primary"}
            disabled={!confirmValid || command.isPending}
            onClick={submitConfirm}
          >
            تأیید
          </Button>
        </DialogActions>
      </Dialog>

      <StorefrontConflictDialog
        open={!!conflict}
        busy={command.isPending}
        reloadError={conflictReloadError}
        onClose={() => { setConflict(null); setConflictReloadError(false); }}
        onReload={() => void Promise.all([customerQuery.refetch(), ordersQuery.refetch()]).then(() => {
          setConflict(null);
          setConflictReloadError(false);
        })}
        onReapply={() => {
          if (conflict) command.mutate(conflict);
          setConflict(null);
          setConflictReloadError(false);
        }}
      />

      {msgOpen && (
        <DirectMessageDialog shopId={shop.id} customerId={customerId} onClose={() => setMsgOpen(false)} />
      )}
    </Box>
  );
}

// A signed Toman delta with an explicit +/− (fmtToman itself is unsigned/absolute-friendly).
function fmtSignedToman(delta: number): string {
  return `${delta >= 0 ? "+" : "−"}${fmtToman(Math.abs(delta))}`;
}

function successMessage(variables: EntityCommand, result: Versioned<unknown>): string {
  if (variables.type === "ban") return variables.banned ? "مشتری مسدود شد." : "مسدودیت مشتری برداشته شد.";
  if (variables.type === "enabled") return variables.enabled ? "سرویس فعال شد." : "سرویس متوقف شد.";
  if (variables.type === "delete") return "سرویس حذف شد.";
  const order = (result.data as OrderRenewResult | undefined)?.order;
  return order
    ? `سرویس تمدید شد (${fmtGb(order.gb)} · ${fmtNum(order.days)} روز).`
    : "سرویس تمدید شد.";
}

function confirmTitle(action: Confirm | null): string {
  if (!action) return "";
  if (action.kind === "ban") return action.banned ? "مسدودسازی مشتری" : "رفع مسدودی مشتری";
  if (action.kind === "renew") return "تمدید رایگان سرویس";
  if (action.kind === "enabled") return action.enabled ? "فعال‌سازی سرویس" : "توقف سرویس";
  return "حذف سرویس";
}

function confirmBody(action: Confirm | null): string {
  if (!action) return "";
  if (action.kind === "ban") {
    return action.banned
      ? "این مشتری از خرید و استفاده در ربات فروشگاه مسدود می‌شود. دلیل برای ثبت در سابقه الزامی است."
      : "مسدودیت این مشتری برداشته می‌شود و دوباره می‌تواند از فروشگاه استفاده کند. دلیل الزامی است.";
  }
  if (action.kind === "renew") {
    return "این سرویس به‌صورت رایگان با حجم و مدت پلن فعلی تمدید می‌شود. مبلغی از مشتری کسر نمی‌شود.";
  }
  if (action.kind === "enabled") {
    return action.enabled
      ? "این سرویس روی پنل دوباره فعال می‌شود."
      : "این سرویس روی پنل موقتاً متوقف می‌شود؛ اتصال کاربر قطع خواهد شد.";
  }
  return "این سرویس روی پنل حذف می‌شود و قابل بازگشت نیست. سابقهٔ مالی حفظ می‌ماند.";
}

function BackButton({ onBack }: { onBack: () => void }) {
  return (
    <Button startIcon={<ArrowForwardIcon />} onClick={onBack} sx={{ mb: 2 }}>
      بازگشت به فهرست مشتریان
    </Button>
  );
}

function InfoRow({ label, value, ltr }: { label: string; value: string; ltr?: boolean }) {
  return (
    <Stack direction="row" justifyContent="space-between" spacing={2}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography
        variant="body2"
        sx={{ fontWeight: 700 }}
        dir={ltr ? "ltr" : undefined}
      >
        {value}
      </Typography>
    </Stack>
  );
}

function OrderCardView({
  order, live, busy, refreshing, refreshDisabled, onRefresh, onRenew, onToggle, onDelete,
}: {
  order: OrderCard;
  live: LiveEntry | undefined;
  busy: boolean;
  refreshing: boolean;
  refreshDisabled: boolean;
  onRefresh: () => void;
  onRenew: () => void;
  onToggle: (enabled: boolean) => void;
  onDelete: () => void;
}) {
  const canManage = order.status !== "deleted";
  const isDisabled = order.status === "disabled";
  return (
    <Card>
      <CardContent>
        <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" spacing={1.5}>
          <Box sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
              <Typography sx={{ fontWeight: 800 }}>{order.label || `سرویس #${fmtNum(order.id)}`}</Typography>
              <Chip size="small" color={orderStatusColor(order.status)} variant="outlined" label={ORDER_STATUS_FA[order.status] || order.status} />
              {order.is_trial && <Chip size="small" variant="outlined" label="آزمایشی" />}
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {fmtGb(order.gb)} · {fmtNum(order.days)} روز · {fmtToman(order.price_toman)}
            </Typography>
          </Box>
          <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
            ایجاد: {fmtDateTime(order.created_at)}
          </Typography>
        </Stack>

        <Divider sx={{ my: 1.5 }} />

        <Box>
          <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>
            آخرین وضعیت ذخیره‌شده
          </Typography>
          {order.snapshot ? (
            <Typography variant="body2" sx={{ mt: 0.3 }}>
              مصرف {fmtGb(order.snapshot.used_gb)} از {fmtGb(order.snapshot.limit_gb)} ·
              {order.snapshot.enabled ? " فعال" : " غیرفعال"} ·
              شروع: {order.snapshot.start_date ? fmtDateTime(order.snapshot.start_date) : "—"}
            </Typography>
          ) : (
            <Typography variant="body2" color="text.disabled" sx={{ mt: 0.3 }}>
              هنوز داده‌ای همگام نشده است.
            </Typography>
          )}
        </Box>

        {live?.result && (
          <Alert severity={live.result.status.ok ? "success" : "warning"} sx={{ mt: 1.5 }}>
            <Typography variant="body2" sx={{ fontWeight: 700 }}>
              وضعیت زنده ({FRESHNESS_FA[live.result.freshness] || live.result.freshness})
            </Typography>
            {live.result.status.ok ? (
              <Typography variant="body2">
                مصرف {fmtGb(live.result.status.used_gb || 0)} از {fmtGb(live.result.status.limit_gb || 0)}
                {live.result.status.remaining_days != null ? ` · ${fmtNum(live.result.status.remaining_days)} روز باقی‌مانده` : ""}
              </Typography>
            ) : (
              <Typography variant="body2">سرویس روی پنل یافت نشد یا در دسترس نیست.</Typography>
            )}
          </Alert>
        )}
        {live?.retryAfter != null && (
          <Alert severity="info" sx={{ mt: 1.5 }}>
            چند لحظه دیگر تلاش کنید ({fmtNum(live.retryAfter)} ثانیه).
          </Alert>
        )}
        {live?.error && (
          <Alert severity="error" sx={{ mt: 1.5 }}>دریافت وضعیت زنده ناموفق بود؛ دوباره تلاش کنید.</Alert>
        )}

        <Stack direction="row" spacing={1} flexWrap="wrap" sx={{ mt: 1.5, gap: 1 }}>
          <Button
            size="small" variant="outlined" startIcon={<RefreshIcon />}
            disabled={refreshDisabled} onClick={onRefresh}
          >
            {refreshing ? "در حال بررسی…" : "به‌روزرسانی وضعیت زنده"}
          </Button>
          {canManage && (
            <>
              <Button size="small" variant="outlined" startIcon={<AutorenewIcon />} disabled={busy} onClick={onRenew}>
                تمدید رایگان
              </Button>
              <Button
                size="small" variant="outlined"
                startIcon={isDisabled ? <PlayCircleOutlineIcon /> : <PauseCircleOutlineIcon />}
                disabled={busy} onClick={() => onToggle(isDisabled)}
              >
                {isDisabled ? "فعال‌سازی" : "توقف موقت"}
              </Button>
              <Button size="small" variant="outlined" color="error" startIcon={<DeleteOutlineIcon />} disabled={busy} onClick={onDelete}>
                حذف سرویس
              </Button>
            </>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}

const LEDGER_KINDS: Array<keyof typeof LEDGER_KIND_FA> = [
  "topup", "purchase", "manual_credit", "manual_debit", "refund", "renew_reversal", "credit_bonus",
];
const LEDGER_STATUSES: Array<keyof typeof LEDGER_STATUS_FA> = ["pending", "confirmed", "rejected", "done"];

function LedgerSection({ shopId, customerId }: { shopId: number; customerId: number }) {
  const [kind, setKind] = useState<string>("all");
  const [status, setStatus] = useState<string>("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  const filters = useMemo<LedgerFilters>(() => ({
    kind: kind === "all" ? undefined : (kind as LedgerFilters["kind"]),
    status: status === "all" ? undefined : (status as LedgerFilters["status"]),
    from: from || undefined,
    to: to || undefined,
  }), [kind, status, from, to]);

  const query = useInfiniteQuery({
    queryKey: storefrontQueryKeys.customerLedger(shopId, customerId, filters),
    queryFn: ({ pageParam }) => getCustomerLedger(shopId, customerId, filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <SectionCard title="سابقهٔ مالی">
      <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} sx={{ mb: 2 }}>
        <TextField select size="small" label="نوع" value={kind} onChange={(event) => setKind(event.target.value)} sx={{ minWidth: { xs: "100%", md: 170 } }}>
          <MenuItem value="all">همه</MenuItem>
          {LEDGER_KINDS.map((value) => <MenuItem key={value} value={value}>{LEDGER_KIND_FA[value]}</MenuItem>)}
        </TextField>
        <TextField select size="small" label="وضعیت" value={status} onChange={(event) => setStatus(event.target.value)} sx={{ minWidth: { xs: "100%", md: 150 } }}>
          <MenuItem value="all">همه</MenuItem>
          {LEDGER_STATUSES.map((value) => <MenuItem key={value} value={value}>{LEDGER_STATUS_FA[value]}</MenuItem>)}
        </TextField>
        <TextField
          size="small" type="date" label="از تاریخ" value={from}
          onChange={(event) => setFrom(event.target.value)}
          InputLabelProps={{ shrink: true }} inputProps={{ dir: "ltr" }}
        />
        <TextField
          size="small" type="date" label="تا تاریخ" value={to}
          onChange={(event) => setTo(event.target.value)}
          InputLabelProps={{ shrink: true }} inputProps={{ dir: "ltr" }}
        />
      </Stack>

      <DataState isLoading={query.isLoading} isError={query.isError} rows={4} onRetry={() => query.refetch()}>
        {!rows.length ? (
          <Alert severity="info">تراکنشی با این فیلترها یافت نشد.</Alert>
        ) : (
          <Stack spacing={1}>
            {rows.map((row) => <LedgerRowView key={row.id} row={row} />)}
            {query.hasNextPage && (
              <Button
                variant="outlined" onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage} sx={{ alignSelf: "center", mt: 1 }}
              >
                {query.isFetchingNextPage ? "در حال بارگذاری…" : "نمایش بیشتر"}
              </Button>
            )}
          </Stack>
        )}
      </DataState>
    </SectionCard>
  );
}

function LedgerRowView({ row }: { row: LedgerRow }) {
  const positive = row.amount_toman >= 0;
  return (
    <Box sx={{ p: 1.25, borderRadius: 2, border: 1, borderColor: "divider" }}>
      <Stack direction="row" justifyContent="space-between" spacing={2} alignItems="flex-start">
        <Box sx={{ minWidth: 0 }}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
            <Typography variant="body2" sx={{ fontWeight: 700 }}>
              {LEDGER_KIND_FA[row.kind] || row.kind}
            </Typography>
            <Chip
              size="small" variant="outlined"
              color={row.status === "confirmed" || row.status === "done" ? "success" : row.status === "rejected" ? "error" : "default"}
              label={LEDGER_STATUS_FA[row.status] || row.status}
            />
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            {fmtDateTime(row.created_at)}
            {row.method ? ` · ${row.method}` : ""}
            {row.note ? ` · ${row.note}` : ""}
          </Typography>
          {row.txid && (
            <Typography variant="caption" color="text.secondary" dir="ltr" sx={{ display: "block", textAlign: "right", wordBreak: "break-all" }}>
              {row.chain ? `${row.chain}: ` : ""}{row.txid}
            </Typography>
          )}
        </Box>
        <Typography
          variant="body2"
          sx={{ fontWeight: 800, whiteSpace: "nowrap", color: positive ? "success.main" : "error.main" }}
        >
          {positive ? "+" : ""}{fmtToman(row.amount_toman)}
        </Typography>
      </Stack>
    </Box>
  );
}

const MSG_MAX = 4000;

function DirectMessageDialog({
  shopId, customerId, onClose,
}: { shopId: number; customerId: number; onClose: () => void }) {
  const xsFull = useXsFullScreen();
  const [text, setText] = useState("");
  const [sent, setSent] = useState(false);
  const mut = useIdempotentMutation<Versioned<DirectMessageResult>, { text: string }>(
    (v, key) => sendDirectMessage(shopId, customerId, v.text, key),
    { onSuccess: () => setSent(true) },
  );
  const len = text.trim().length;
  const rateLimited = rateLimitRetryAfter(mut.error) != null
    || (mut.error as { response?: { data?: { detail?: { code?: string } } } })
      ?.response?.data?.detail?.code === "rate_limited";

  return (
    <Dialog open onClose={() => !mut.isPending && onClose()} maxWidth="xs" fullWidth fullScreen={xsFull}>
      <DialogTitle>ارسال پیام به مشتری</DialogTitle>
      <DialogContent>
        {sent ? (
          <Alert severity="success">پیام در صف ارسال قرار گرفت و به‌زودی برای مشتری ارسال می‌شود.</Alert>
        ) : (
          <Stack spacing={1.5} sx={{ mt: 0.5 }}>
            <TextField autoFocus label="متن پیام" value={text} onChange={(e) => setText(e.target.value)}
              multiline minRows={3} fullWidth error={len > MSG_MAX}
              helperText={`${fmtNum(len)} / ${fmtNum(MSG_MAX)} نویسه`} />
            {rateLimited && (
              <Alert severity="warning">در دقیقهٔ اخیر پیام‌های زیادی فرستاده‌اید؛ کمی بعد دوباره تلاش کنید.</Alert>
            )}
            {mut.isError && !rateLimited && <Alert severity="error">ارسال ناموفق بود.</Alert>}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={mut.isPending}>{sent ? "بستن" : "انصراف"}</Button>
        {!sent && (
          <Button variant="contained" disabled={mut.isPending || len < 1 || len > MSG_MAX}
            onClick={() => mut.mutate({ text: text.trim() })}>
            {mut.isPending ? "در حال ارسال…" : "ارسال"}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
