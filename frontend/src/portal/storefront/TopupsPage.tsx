import { useEffect, useMemo, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogContentText, DialogTitle, MenuItem, Stack, Tab, Tabs, TextField,
  Typography,
} from "@mui/material";
import DoneIcon from "@mui/icons-material/esm/Done";
import CloseIcon from "@mui/icons-material/esm/Close";
import EditIcon from "@mui/icons-material/esm/Edit";
import ReceiptIcon from "@mui/icons-material/esm/Receipt";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtDateTime, fmtNum, fmtToman } from "../../format";
import {
  bulkDecideTopups, currentTehranMonthRange, decideTopup, getStorefrontDashboard, getTopup,
  getTopupProof, listTopups, storefrontQueryKeys,
} from "./api";
import StorefrontConflictDialog from "./StorefrontConflictDialog";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type {
  BulkDecisionBody, BulkDecisionResult, TopupDecisionBody, TopupDecisionResult, TopupListFilters,
  TopupListItem, TopupMethod, TopupStatus, Versioned,
} from "./types";
import { isConflict, isNotFound, storefrontErrorMessage, useIdempotentMutation } from "./mutation";
import { useXsFullScreen } from "../../responsive";
import { NumberField } from "../../components/NumberField";

const TOPUP_STATUS_FA: Record<string, string> = {
  pending: "در انتظار",
  confirmed: "تأییدشده",
  rejected: "ردشده",
};
const TOPUP_METHOD_FA: Record<string, string> = {
  card: "کارت به کارت",
  usdt: "USDT",
  ton: "تون (TON)",
};
const BULK_RESULT_FA: Record<string, string> = {
  changed: "اعمال شد",
  already_decided: "قبلاً اعمال شده",
  not_found: "یافت نشد",
  failed: "ناموفق",
};
const topupStatusColor = (s: string): "warning" | "success" | "error" | "default" =>
  s === "confirmed" ? "success" : s === "rejected" ? "error" : s === "pending" ? "warning" : "default";

const MAX_BULK = 100;
const REASON_MIN = 3;
const REASON_MAX = 255;
const AMOUNT_MAX = 10 ** 12;
const reasonValid = (r: string) => r.trim().length >= REASON_MIN && r.trim().length <= REASON_MAX;
// A query reaches the server only when it is an all-digit id/txid or ≥3 chars; shorter free text is
// gated locally so we never trigger the 422 the backend raises for a too-short query.
const isQueryUsable = (q: string) => q.length === 0 || /^\d+$/.test(q) || q.length >= 3;

type TabValue = "queue" | "history";
type StatusFilter = "all" | TopupStatus;
type MethodFilter = "all" | TopupMethod;

// The requested (customer's ask) vs credited (what actually landed) amounts; `corrected` when a
// confirm changed the credited amount away from the request.
function amountView(t: TopupListItem) {
  const requested = t.requested_amount_toman ?? t.amount_toman;
  const credited = t.amount_toman;
  const corrected = t.status === "confirmed" && t.requested_amount_toman != null && requested !== credited;
  return { requested, credited, corrected };
}

type ActiveDecision = { kind: "confirm" | "correct" | "reject"; txn: TopupListItem };
type PendingCommand =
  | { type: "decision"; txnId: number; body: TopupDecisionBody }
  | { type: "bulk"; body: BulkDecisionBody };

export default function TopupsPage() {
  const xsFull = useXsFullScreen();
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<TabValue>("queue");
  const [method, setMethod] = useState<MethodFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [minAmount, setMinAmount] = useState("");
  const [maxAmount, setMaxAmount] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [q, setQ] = useState("");

  const trimmedQ = q.trim();
  const queryUsable = isQueryUsable(trimmedQ);
  const status: TopupStatus | undefined = tab === "queue"
    ? "pending"
    : statusFilter === "all" ? undefined : statusFilter;

  const filters = useMemo<TopupListFilters>(() => ({
    status,
    method: method === "all" ? undefined : method,
    min_amount: minAmount ? Number(minAmount) : undefined,
    max_amount: maxAmount ? Number(maxAmount) : undefined,
    from: from || undefined,
    to: to || undefined,
    q: queryUsable && trimmedQ ? trimmedQ : undefined,
  }), [status, method, minAmount, maxAmount, from, to, queryUsable, trimmedQ]);

  const query = useInfiniteQuery({
    queryKey: storefrontQueryKeys.topups(shop.id, filters),
    queryFn: ({ pageParam }) => listTopups(shop.id, filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const topups = query.data?.pages.flatMap((page) => page.items) ?? [];

  // Live pending count (shared cache with the dashboard tab + shell badge).
  const range = currentTehranMonthRange();
  const pendingCountQuery = useQuery({
    queryKey: storefrontQueryKeys.dashboard(shop.id, range.from, range.to),
    queryFn: () => getStorefrontDashboard(shop.id, range.from, range.to),
    select: (d) => d.pending_topups.count,
  });
  const pendingCount = pendingCountQuery.data ?? 0;

  // Bulk selection is bounded (≤100) and only in the queue; it resets on any filter/tab change.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const filterKey = JSON.stringify(filters);
  useEffect(() => setSelected(new Set()), [filterKey]);

  const [active, setActive] = useState<ActiveDecision | null>(null);
  const [reason, setReason] = useState("");
  const [corrected, setCorrected] = useState("");
  const [bulkAction, setBulkAction] = useState<{ decision: "confirm" | "reject" } | null>(null);
  const [bulkReason, setBulkReason] = useState("");
  const [bulkResult, setBulkResult] = useState<BulkDecisionResult | null>(null);
  const [detailTxn, setDetailTxn] = useState<TopupListItem | null>(null);
  const [conflict, setConflict] = useState<PendingCommand | null>(null);
  const [conflictReloadError, setConflictReloadError] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const invalidateAfter = (txnId?: number) => Promise.all([
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "topups"] }),
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "dashboard"] }),
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "customer"] }),
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "customers"] }),
    txnId
      ? queryClient.invalidateQueries({ queryKey: storefrontQueryKeys.topup(shop.id, txnId) })
      : Promise.resolve(),
  ]);

  const decision = useIdempotentMutation<Versioned<TopupDecisionResult>, { txnId: number; body: TopupDecisionBody }>(
    (input, key) => decideTopup(shop.id, input.txnId, input.body, key),
    {
      onSuccess: async (result, variables) => {
        await invalidateAfter(variables.txnId);
        setActive(null);
        setReason("");
        setCorrected("");
        setMessage(decisionMessage(result.data));
      },
      onError: (error, variables) => {
        if (isConflict(error)) setConflict({ type: "decision", ...variables });
      },
    },
  );

  const bulk = useIdempotentMutation<Versioned<BulkDecisionResult>, BulkDecisionBody>(
    (input, key) => bulkDecideTopups(shop.id, input, key),
    {
      onSuccess: async (result) => {
        await invalidateAfter();
        setBulkAction(null);
        setBulkReason("");
        setSelected(new Set());
        setBulkResult(result.data);
      },
      onError: (error, variables) => {
        if (isConflict(error)) setConflict({ type: "bulk", body: variables });
      },
    },
  );

  const busy = decision.isPending || bulk.isPending;
  const actionError = mutationError(decision.error, decision.isError)
    || mutationError(bulk.error, bulk.isError);

  const toggleSelected = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else if (next.size < MAX_BULK) next.add(id);
    return next;
  });
  const selectAllVisible = () => setSelected((prev) => {
    const next = new Set(prev);
    for (const t of topups) {
      if (next.size >= MAX_BULK) break;
      if (t.status === "pending") next.add(t.id);
    }
    return next;
  });

  const correctedNum = Number(corrected);
  const correctedValid = corrected !== "" && Number.isFinite(correctedNum)
    && correctedNum >= 1 && correctedNum <= AMOUNT_MAX;
  const activeValid = !active ? false
    : active.kind === "confirm" ? true
      : active.kind === "reject" ? reasonValid(reason)
        : correctedValid && reasonValid(reason);

  const submitActive = () => {
    if (!active || !activeValid || busy) return;
    const body: TopupDecisionBody = active.kind === "confirm"
      ? { decision: "confirm" }
      : active.kind === "reject"
        ? { decision: "reject", reason: reason.trim() }
        : { decision: "confirm", corrected_amount: Math.round(correctedNum), reason: reason.trim() };
    decision.mutate({ txnId: active.txn.id, body });
  };

  const bulkValid = !bulkAction ? false : bulkAction.decision === "confirm" || reasonValid(bulkReason);
  const submitBulk = () => {
    if (!bulkAction || !bulkValid || busy || selected.size === 0) return;
    const items = [...selected].map((id) => ({ txn_id: id, decision: bulkAction.decision }));
    bulk.mutate({
      items,
      reason: bulkAction.decision === "reject" ? bulkReason.trim() : undefined,
    });
  };

  const openDecision = (kind: ActiveDecision["kind"], txn: TopupListItem) => {
    setReason("");
    setCorrected(kind === "correct" ? String(txn.requested_amount_toman ?? txn.amount_toman) : "");
    setActive({ kind, txn });
  };

  return (
    <Box>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <Typography variant="h6">شارژها و تراکنش‌ها</Typography>
        <Typography variant="body2" color="text.secondary">
          شارژهای کیف پول مشتریان را بررسی، تأیید یا رد کنید.
        </Typography>
      </Stack>

      {message && <Alert severity="success" onClose={() => setMessage(null)} sx={{ mb: 2 }}>{message}</Alert>}
      {actionError && <Alert severity="error" sx={{ mb: 2 }}>{actionError}</Alert>}

      <Tabs
        value={tab}
        onChange={(_, value) => setTab(value as TabValue)}
        sx={{ mb: 2, borderBottom: 1, borderColor: "divider" }}
      >
        <Tab value="queue" label={<TabLabel text="صف بررسی" count={pendingCount} />} />
        <Tab value="history" label="تاریخچه" />
      </Tabs>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} flexWrap="wrap" useFlexGap>
            <TextField
              size="small" label="جست‌وجو (نام، شناسه یا شناسهٔ تراکنش)" value={q}
              onChange={(event) => setQ(event.target.value)}
              error={!!trimmedQ && !queryUsable}
              helperText={!!trimmedQ && !queryUsable
                ? "حداقل ۳ نویسه یا یک شناسهٔ عددی وارد کنید."
                : " "}
              sx={{ minWidth: { xs: "100%", md: 260 } }}
            />
            {tab === "history" && (
              <TextField
                select size="small" label="وضعیت" value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
                sx={{ minWidth: { xs: "100%", md: 140 } }}
              >
                <MenuItem value="all">همه</MenuItem>
                <MenuItem value="pending">در انتظار</MenuItem>
                <MenuItem value="confirmed">تأییدشده</MenuItem>
                <MenuItem value="rejected">ردشده</MenuItem>
              </TextField>
            )}
            <TextField
              select size="small" label="روش" value={method}
              onChange={(event) => setMethod(event.target.value as MethodFilter)}
              sx={{ minWidth: { xs: "100%", md: 150 } }}
            >
              <MenuItem value="all">همه</MenuItem>
              <MenuItem value="card">کارت به کارت</MenuItem>
              <MenuItem value="usdt">USDT</MenuItem>
              <MenuItem value="ton">تون (TON)</MenuItem>
            </TextField>
            <NumberField
              size="small" label="حداقل مبلغ" value={minAmount}
              onChange={setMinAmount} sx={{ minWidth: { xs: "100%", md: 130 } }}
            />
            <NumberField
              size="small" label="حداکثر مبلغ" value={maxAmount}
              onChange={setMaxAmount} sx={{ minWidth: { xs: "100%", md: 130 } }}
            />
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
        </CardContent>
      </Card>

      {tab === "queue" && selected.size > 0 && (
        <Card sx={{ mb: 2, bgcolor: "action.hover" }}>
          <CardContent sx={{ "&:last-child": { pb: 2 } }}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }} justifyContent="space-between">
              <Typography variant="body2" sx={{ fontWeight: 700 }}>
                {fmtNum(selected.size)} مورد انتخاب شده (حداکثر {fmtNum(MAX_BULK)})
              </Typography>
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                <Button size="small" onClick={() => setSelected(new Set())} disabled={busy}>پاک کردن انتخاب</Button>
                <Button size="small" variant="contained" color="success" startIcon={<DoneIcon />} disabled={busy} onClick={() => { setBulkReason(""); setBulkAction({ decision: "confirm" }); }}>
                  تأیید گروهی
                </Button>
                <Button size="small" variant="contained" color="error" startIcon={<CloseIcon />} disabled={busy} onClick={() => { setBulkReason(""); setBulkAction({ decision: "reject" }); }}>
                  رد گروهی
                </Button>
              </Stack>
            </Stack>
          </CardContent>
        </Card>
      )}

      <DataState isLoading={query.isLoading} isError={query.isError} rows={6} onRetry={() => query.refetch()}>
        {!topups.length ? (
          <Alert severity="info">
            {tab === "queue" ? "شارژ در انتظاری برای بررسی وجود ندارد." : "تراکنشی با این فیلترها یافت نشد."}
          </Alert>
        ) : (
          <Stack spacing={1.25}>
            {tab === "queue" && topups.some((t) => t.status === "pending") && (
              <Button size="small" onClick={selectAllVisible} disabled={busy} sx={{ alignSelf: "flex-start" }}>
                انتخاب موارد این صفحه
              </Button>
            )}
            {topups.map((topup) => (
              <TopupRow
                key={topup.id}
                topup={topup}
                selectable={tab === "queue"}
                selected={selected.has(topup.id)}
                selectDisabled={busy || (!selected.has(topup.id) && selected.size >= MAX_BULK)}
                busy={busy}
                onToggleSelect={() => toggleSelected(topup.id)}
                onConfirm={() => openDecision("confirm", topup)}
                onCorrect={() => openDecision("correct", topup)}
                onReject={() => openDecision("reject", topup)}
                onView={() => setDetailTxn(topup)}
              />
            ))}
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

      {/* single-row decision */}
      <Dialog open={!!active} onClose={() => !busy && setActive(null)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <DialogTitle>{decisionTitle(active)}</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>{decisionBody(active)}</DialogContentText>
          {active?.kind === "correct" && (
            <NumberField
              autoFocus fullWidth label="مبلغ اصلاح‌شده (تومان)" value={corrected}
              onChange={setCorrected}
              error={corrected !== "" && !correctedValid}
              helperText="مبلغی که واقعاً به کیف پول افزوده می‌شود"
              disabled={busy} sx={{ mb: 2 }}
            />
          )}
          {(active?.kind === "reject" || active?.kind === "correct") && (
            <TextField
              autoFocus={active?.kind === "reject"} fullWidth label="دلیل (الزامی)" value={reason}
              onChange={(event) => setReason(event.target.value)}
              error={!!reason && !reasonValid(reason)}
              helperText="بین ۳ تا ۲۵۵ نویسه"
              inputProps={{ maxLength: REASON_MAX }} disabled={busy}
            />
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setActive(null)} disabled={busy}>انصراف</Button>
          <Button
            variant="contained"
            color={active?.kind === "reject" ? "error" : "primary"}
            disabled={!activeValid || busy}
            onClick={submitActive}
          >
            تأیید
          </Button>
        </DialogActions>
      </Dialog>

      {/* bulk decision */}
      <Dialog open={!!bulkAction} onClose={() => !busy && setBulkAction(null)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <DialogTitle>{bulkAction?.decision === "reject" ? "رد گروهی شارژها" : "تأیید گروهی شارژها"}</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: bulkAction?.decision === "reject" ? 2 : 0 }}>
            {bulkAction?.decision === "reject"
              ? `${fmtNum(selected.size)} شارژ انتخاب‌شده رد می‌شود. این کار قابل بازگشت نیست.`
              : `${fmtNum(selected.size)} شارژ انتخاب‌شده تأیید و مبلغ هرکدام به کیف پول مشتری افزوده می‌شود.`}
          </DialogContentText>
          {bulkAction?.decision === "reject" && (
            <TextField
              autoFocus fullWidth label="دلیل رد (الزامی)" value={bulkReason}
              onChange={(event) => setBulkReason(event.target.value)}
              error={!!bulkReason && !reasonValid(bulkReason)}
              helperText="بین ۳ تا ۲۵۵ نویسه — برای همهٔ موارد اعمال می‌شود"
              inputProps={{ maxLength: REASON_MAX }} disabled={busy}
            />
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setBulkAction(null)} disabled={busy}>انصراف</Button>
          <Button
            variant="contained"
            color={bulkAction?.decision === "reject" ? "error" : "success"}
            disabled={!bulkValid || busy}
            onClick={submitBulk}
          >
            {busy ? "در حال اعمال…" : "اعمال"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* bulk results */}
      <Dialog open={!!bulkResult} onClose={() => setBulkResult(null)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <DialogTitle>نتیجهٔ بررسی گروهی</DialogTitle>
        <DialogContent>
          {bulkResult && (
            <Stack spacing={1.5}>
              <Alert severity={bulkResult.counts.failed > 0 ? "warning" : "success"}>
                {fmtNum(bulkResult.counts.changed + bulkResult.counts.already_decided)} مورد با موفقیت اعمال شد
                {bulkResult.counts.already_decided > 0
                  ? ` (${fmtNum(bulkResult.counts.already_decided)} مورد پیش‌تر اعمال شده بود)`
                  : ""}
                {bulkResult.counts.failed > 0 ? ` · ${fmtNum(bulkResult.counts.failed)} ناموفق` : ""}
                {bulkResult.counts.not_found > 0 ? ` · ${fmtNum(bulkResult.counts.not_found)} یافت‌نشده` : ""}
              </Alert>
              <Stack spacing={0.75}>
                {bulkResult.results.map((r) => (
                  <Stack key={r.txn_id} direction="row" justifyContent="space-between" alignItems="center">
                    <Typography variant="body2" dir="ltr">#{fmtNum(r.txn_id)}</Typography>
                    <Chip
                      size="small" variant="outlined"
                      color={r.result === "changed" || r.result === "already_decided" ? "success" : r.result === "not_found" ? "warning" : "error"}
                      label={BULK_RESULT_FA[r.result] || r.result}
                    />
                  </Stack>
                ))}
              </Stack>
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button variant="contained" onClick={() => setBulkResult(null)}>بستن</Button>
        </DialogActions>
      </Dialog>

      {detailTxn && (
        <TopupDetailDialog shopId={shop.id} topup={detailTxn} onClose={() => setDetailTxn(null)} />
      )}

      <StorefrontConflictDialog
        open={!!conflict}
        busy={busy}
        reloadError={conflictReloadError}
        onClose={() => { setConflict(null); setConflictReloadError(false); }}
        onReload={() => void query.refetch().then(() => { setConflict(null); setConflictReloadError(false); })
          .catch(() => setConflictReloadError(true))}
        onReapply={() => {
          const pending = conflict;
          setConflict(null);
          setConflictReloadError(false);
          if (pending?.type === "decision") decision.mutate({ txnId: pending.txnId, body: pending.body });
          else if (pending?.type === "bulk") bulk.mutate(pending.body);
        }}
      />
    </Box>
  );
}

function decisionMessage(result: TopupDecisionResult): string {
  if (result.already_decided) return "این شارژ پیش‌تر تعیین‌تکلیف شده بود.";
  if (result.decision === "confirm") {
    return result.credited != null
      ? `شارژ تأیید شد؛ ${fmtToman(result.credited)} به کیف پول افزوده شد.`
      : "شارژ تأیید شد.";
  }
  return "شارژ رد شد.";
}

function mutationError(error: unknown, isError: boolean): string | null {
  if (!isError || isConflict(error)) return null;
  // 404 keeps its own wording — "this top-up is gone" is more useful here than the generic one.
  if (isNotFound(error)) return "این شارژ دیگر در دسترس نیست؛ فهرست را تازه‌سازی کنید.";
  return storefrontErrorMessage(error, "انجام عملیات ناموفق بود؛ دوباره تلاش کنید.");
}

function decisionTitle(active: ActiveDecision | null): string {
  if (!active) return "";
  if (active.kind === "confirm") return "تأیید شارژ";
  if (active.kind === "correct") return "تأیید با اصلاح مبلغ";
  return "رد شارژ";
}
function decisionBody(active: ActiveDecision | null): string {
  if (!active) return "";
  const { requested } = amountView(active.txn);
  if (active.kind === "confirm") {
    return `مبلغ درخواستی ${fmtToman(requested)} به کیف پول مشتری افزوده می‌شود.`;
  }
  if (active.kind === "correct") {
    return `مبلغ درخواستی مشتری ${fmtToman(requested)} بود. مبلغ صحیح را وارد و دلیل را ثبت کنید.`;
  }
  return "این شارژ رد می‌شود و مبلغی به کیف پول افزوده نمی‌شود. دلیل الزامی است.";
}

function TabLabel({ text, count }: { text: string; count: number }) {
  return (
    <Stack direction="row" spacing={0.75} alignItems="center">
      <span>{text}</span>
      {count > 0 && (
        <Box
          component="span"
          sx={{
            bgcolor: "error.main", color: "error.contrastText", borderRadius: 980,
            px: 0.9, minWidth: 20, textAlign: "center", fontSize: 12, fontWeight: 800, lineHeight: "20px",
          }}
        >
          {fmtNum(count)}
        </Box>
      )}
    </Stack>
  );
}

function TopupRow({
  topup, selectable, selected, selectDisabled, busy,
  onToggleSelect, onConfirm, onCorrect, onReject, onView,
}: {
  topup: TopupListItem;
  selectable: boolean;
  selected: boolean;
  selectDisabled: boolean;
  busy: boolean;
  onToggleSelect: () => void;
  onConfirm: () => void;
  onCorrect: () => void;
  onReject: () => void;
  onView: () => void;
}) {
  const cust = topup.customer;
  const title = cust.name?.trim()
    || (cust.username ? `@${cust.username.replace(/^@/, "")}` : `کاربر ${fmtNum(cust.telegram_id)}`);
  const { requested, credited, corrected } = amountView(topup);
  const isPending = topup.status === "pending";
  return (
    <Card>
      <CardContent sx={{ "&:last-child": { pb: 2 } }}>
        <Stack direction="row" spacing={1.5} alignItems="flex-start">
          {selectable && (
            <Checkbox
              checked={selected}
              disabled={!isPending || selectDisabled}
              onChange={onToggleSelect}
              inputProps={{ "aria-label": `انتخاب شارژ ${fmtNum(topup.id)}` }}
              sx={{ mt: -0.5 }}
            />
          )}
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography sx={{ fontWeight: 800 }} noWrap>{title}</Typography>
              <Chip size="small" variant="outlined" color={topupStatusColor(topup.status)} label={TOPUP_STATUS_FA[topup.status] || topup.status} />
              {topup.method && <Chip size="small" variant="outlined" label={TOPUP_METHOD_FA[topup.method] || topup.method} />}
              {topup.has_proof && <Chip size="small" variant="outlined" label="دارای رسید" />}
            </Stack>
            <Typography variant="caption" color="text.secondary" dir="ltr" sx={{ display: "block", textAlign: "right" }}>
              #{fmtNum(topup.id)} · {fmtNum(cust.telegram_id)}
              {cust.username ? ` · @${cust.username.replace(/^@/, "")}` : ""}
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
              {fmtDateTime(topup.created_at)}
              {topup.chain ? ` · ${topup.chain}` : ""}
            </Typography>

            <Box sx={{ mt: 0.75 }}>
              {corrected ? (
                <>
                  <Typography variant="body2" sx={{ fontWeight: 800, color: "success.main" }}>
                    اعتبارشده: {fmtToman(credited)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ textDecoration: "line-through" }}>
                    درخواست: {fmtToman(requested)}
                  </Typography>
                </>
              ) : (
                <Typography variant="body2" sx={{ fontWeight: 800 }}>
                  {topup.status === "confirmed" ? "اعتبارشده: " : "درخواست: "}
                  {fmtToman(topup.status === "confirmed" ? credited : requested)}
                </Typography>
              )}
            </Box>

            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 1.25 }}>
              {isPending && (
                <>
                  <Button size="small" variant="outlined" color="success" startIcon={<DoneIcon />} disabled={busy} onClick={onConfirm}>تأیید</Button>
                  <Button size="small" variant="outlined" startIcon={<EditIcon />} disabled={busy} onClick={onCorrect}>تأیید با اصلاح مبلغ</Button>
                  <Button size="small" variant="outlined" color="error" startIcon={<CloseIcon />} disabled={busy} onClick={onReject}>رد</Button>
                </>
              )}
              <Button size="small" startIcon={<ReceiptIcon />} onClick={onView}>مشاهدهٔ جزئیات</Button>
            </Stack>
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}

function TopupDetailDialog({
  shopId, topup, onClose,
}: { shopId: number; topup: TopupListItem; onClose: () => void }) {
  const xsFull = useXsFullScreen();
  const detailQuery = useQuery({
    queryKey: storefrontQueryKeys.topup(shopId, topup.id),
    queryFn: () => getTopup(shopId, topup.id),
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const proofQuery = useQuery({
    queryKey: ["portal-storefronts", shopId, "topup", topup.id, "proof"],
    queryFn: () => getTopupProof(shopId, topup.id),
    enabled: topup.has_proof,
    retry: false, // a proof is a single artifact; a failed/missing fetch is a definite "not available"
  });

  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!proofQuery.data) return;
    const url = URL.createObjectURL(proofQuery.data);
    setObjectUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [proofQuery.data]);

  const detail = detailQuery.data;
  const { requested, credited, corrected } = amountView(topup);

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth fullScreen={xsFull}>
      <DialogTitle>جزئیات شارژ #{fmtNum(topup.id)}</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={1.25}>
          <DetailRow label="وضعیت" value={TOPUP_STATUS_FA[topup.status] || topup.status} />
          <DetailRow label="روش" value={topup.method ? (TOPUP_METHOD_FA[topup.method] || topup.method) : "—"} />
          <DetailRow label="مبلغ درخواستی" value={fmtToman(requested)} />
          {corrected && <DetailRow label="مبلغ اعتبارشده" value={fmtToman(credited)} />}
          <DetailRow label="تاریخ ثبت" value={fmtDateTime(topup.created_at)} />
          {topup.decided_at && <DetailRow label="تاریخ تصمیم" value={fmtDateTime(topup.decided_at)} />}
          {detail?.note && <DetailRow label="یادداشت" value={detail.note} />}
          {detail?.credit_code_id != null && (
            <DetailRow label="کد اعتبار" value={`#${fmtNum(detail.credit_code_id)}`} ltr />
          )}

          {topup.txid && (
            <Box>
              <Typography variant="caption" color="text.secondary">شناسهٔ تراکنش</Typography>
              <Typography variant="body2" dir="ltr" sx={{ wordBreak: "break-all", textAlign: "right" }}>
                {topup.chain ? `${topup.chain}: ` : ""}{topup.txid}
              </Typography>
            </Box>
          )}

          {topup.has_proof && (
            <Box>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>رسید</Typography>
              {proofQuery.isLoading && <CircularProgress size={22} />}
              {proofQuery.isError && (
                <Alert severity="warning">رسید در دسترس نیست.</Alert>
              )}
              {objectUrl && (
                <Box
                  component="img" src={objectUrl} alt={`رسید شارژ ${fmtNum(topup.id)}`}
                  sx={{ maxWidth: "100%", borderRadius: 2, border: 1, borderColor: "divider" }}
                />
              )}
            </Box>
          )}
          {!topup.has_proof && !topup.txid && (
            <Typography variant="body2" color="text.disabled">رسید یا شناسهٔ تراکنشی ثبت نشده است.</Typography>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button variant="contained" onClick={onClose}>بستن</Button>
      </DialogActions>
    </Dialog>
  );
}

function DetailRow({ label, value, ltr }: { label: string; value: string; ltr?: boolean }) {
  return (
    <Stack direction="row" justifyContent="space-between" spacing={2}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography variant="body2" sx={{ fontWeight: 700 }} dir={ltr ? "ltr" : undefined}>{value}</Typography>
    </Stack>
  );
}
