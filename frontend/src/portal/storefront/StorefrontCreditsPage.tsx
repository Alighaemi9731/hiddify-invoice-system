import { useMemo, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, MenuItem, Stack, Switch, TextField, Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/esm/Add";
import EditIcon from "@mui/icons-material/esm/Edit";
import ArchiveIcon from "@mui/icons-material/esm/Archive";
import InsightsIcon from "@mui/icons-material/esm/Insights";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtDateTime, fmtNum, fmtToman } from "../../format";
import {
  archiveCreditCode, createCreditCode, getCreditUsage, listCreditCodes, listCreditRedemptions,
  setCreditCodeEnabled, storefrontQueryKeys, updateCreditCode,
} from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type { CreditCode, CreditCreateBody, CreditKind, CreditUpdateBody, Versioned } from "./types";
import { isNotFound, storefrontErrorMessage, useIdempotentMutation } from "./mutation";
import { useXsFullScreen } from "../../responsive";
import { NumberField } from "../../components/NumberField";

type FormState = {
  code: string;
  kind: CreditKind;
  is_gift: boolean;
  percent_off: string;
  amount_toman: string;
  max_bonus_toman: string;
  min_topup_toman: string;
  max_uses: string;
  per_customer_limit: string;
  expires_at: string;
  enabled: boolean;
};

const emptyForm = (): FormState => ({
  code: "", kind: "percent", is_gift: false, percent_off: "", amount_toman: "",
  max_bonus_toman: "", min_topup_toman: "", max_uses: "", per_customer_limit: "1",
  expires_at: "", enabled: true,
});

const num = (v: string): number | null => (v.trim() === "" ? null : Number(v));
const isoOrNull = (v: string): string | null => (v.trim() === "" ? null : new Date(v).toISOString());

function kindLabel(c: CreditCode): string {
  if (c.is_gift) return "هدیه";
  return c.kind === "percent" ? "درصدی" : "مبلغ ثابت";
}

function valueLabel(c: CreditCode): string {
  if (c.kind === "percent") {
    return `${fmtNum(c.percent_off ?? 0)}٪${c.max_bonus_toman ? ` (سقف ${fmtToman(c.max_bonus_toman)})` : ""}`;
  }
  return fmtToman(c.amount_toman ?? 0);
}

export default function StorefrontCreditsPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();
  const [includeArchived, setIncludeArchived] = useState(false);
  const [editing, setEditing] = useState<CreditCode | null>(null);
  const [creating, setCreating] = useState(false);
  const [usageFor, setUsageFor] = useState<CreditCode | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const query = useInfiniteQuery({
    queryKey: storefrontQueryKeys.credits(shop.id, includeArchived),
    queryFn: ({ pageParam }) => listCreditCodes(shop.id, includeArchived, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const codes = query.data?.pages.flatMap((p) => p.items) ?? [];

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["portal-storefronts", shop.id, "credits"] });

  const enableMut = useIdempotentMutation<Versioned<{ credit: CreditCode }>, { id: number; enabled: boolean }>(
    (v, key) => setCreditCodeEnabled(shop.id, v.id, v.enabled, key),
    // One lane per code: toggling code A must not block a toggle on code B that's still in flight.
    { commandKey: (v) => String(v.id), onSuccess: async () => { await invalidate(); } },
  );
  const archiveMut = useIdempotentMutation<Versioned<{ credit: CreditCode }>, { id: number }>(
    (v, key) => archiveCreditCode(shop.id, v.id, key),
    { commandKey: (v) => String(v.id), onSuccess: async () => { await invalidate(); setMessage("کد بایگانی شد."); } },
  );

  const busy = enableMut.isPending || archiveMut.isPending;
  // Say which failure it was. "try again" is useless advice for a 404 or a dead shop bot.
  const actionError = enableMut.isError
    ? storefrontErrorMessage(enableMut.error, "انجام عملیات ناموفق بود؛ دوباره تلاش کنید.")
    : archiveMut.isError
      ? storefrontErrorMessage(archiveMut.error, "انجام عملیات ناموفق بود؛ دوباره تلاش کنید.")
      : null;

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between"
        alignItems={{ sm: "center" }} spacing={1.5} sx={{ mb: 2 }}>
        <Box>
          <Typography variant="h6">کدهای شارژ / هدیه</Typography>
          <Typography variant="body2" color="text.secondary">
            کد تخفیف شارژ کیف پول یا هدیهٔ مستقیم برای مشتریان بسازید و مدیریت کنید.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          <FormControlLabel
            control={<Switch size="small" checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)} />}
            label="نمایش بایگانی‌شده‌ها"
          />
          <Button variant="contained" startIcon={<AddIcon />} onClick={() => setCreating(true)}>
            کد جدید
          </Button>
        </Stack>
      </Stack>

      {message && <Alert severity="success" onClose={() => setMessage(null)} sx={{ mb: 2 }}>{message}</Alert>}
      {actionError && <Alert severity="error" sx={{ mb: 2 }}>{actionError}</Alert>}

      <DataState isLoading={query.isLoading} isError={query.isError} rows={5} onRetry={() => query.refetch()}>
        {!codes.length ? (
          <Alert severity="info">هنوز کدی ثبت نشده است.</Alert>
        ) : (
          <Stack spacing={1.25}>
            {codes.map((code) => (
              <Card key={code.id} sx={{ opacity: code.archived ? 0.65 : 1 }}>
                <CardContent sx={{ "&:last-child": { pb: 2 } }}>
                  <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                    <Typography sx={{ fontWeight: 800, fontFamily: "monospace" }} dir="ltr">{code.code}</Typography>
                    <Chip size="small" variant="outlined" label={kindLabel(code)} />
                    <Chip size="small" variant="outlined" color={code.enabled ? "success" : "default"}
                      label={code.enabled ? "فعال" : "غیرفعال"} />
                    {code.archived && <Chip size="small" color="warning" variant="outlined" label="بایگانی" />}
                  </Stack>
                  <Typography variant="body2" sx={{ mt: 0.75 }}>
                    ارزش: <b>{valueLabel(code)}</b>
                    {code.min_topup_toman > 0 ? ` · حداقل شارژ: ${fmtToman(code.min_topup_toman)}` : ""}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25 }}>
                    استفاده‌شده: {fmtNum(code.used_count)}
                    {code.max_uses != null ? ` از ${fmtNum(code.max_uses)}` : ""}
                    {code.per_customer_limit != null ? ` · هر مشتری ${fmtNum(code.per_customer_limit)} بار` : ""}
                    {code.expires_at ? ` · انقضا: ${fmtDateTime(code.expires_at)}` : ""}
                  </Typography>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 1.25 }}>
                    {!code.archived && (
                      <Button size="small" startIcon={<EditIcon />} disabled={busy}
                        onClick={() => setEditing(code)}>ویرایش</Button>
                    )}
                    {!code.archived && (
                      <Button size="small" disabled={busy}
                        onClick={() => enableMut.mutate({ id: code.id, enabled: !code.enabled })}>
                        {code.enabled ? "غیرفعال کردن" : "فعال کردن"}
                      </Button>
                    )}
                    <Button size="small" startIcon={<InsightsIcon />} onClick={() => setUsageFor(code)}>
                      آمار مصرف
                    </Button>
                    {!code.archived && (
                      <Button size="small" color="warning" startIcon={<ArchiveIcon />} disabled={busy}
                        onClick={() => archiveMut.mutate({ id: code.id })}>بایگانی</Button>
                    )}
                  </Stack>
                </CardContent>
              </Card>
            ))}
            {query.hasNextPage && (
              <Button variant="outlined" onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage} sx={{ alignSelf: "center", mt: 1 }}>
                {query.isFetchingNextPage ? "در حال بارگذاری…" : "نمایش بیشتر"}
              </Button>
            )}
          </Stack>
        )}
      </DataState>

      {(creating || editing) && (
        <CreditFormDialog
          shopId={shop.id}
          editing={editing}
          onClose={() => { setCreating(false); setEditing(null); }}
          onDone={(msg) => { setCreating(false); setEditing(null); void invalidate(); setMessage(msg); }}
        />
      )}
      {usageFor && (
        <CreditUsageDialog shopId={shop.id} code={usageFor} onClose={() => setUsageFor(null)} />
      )}
    </Box>
  );
}

function CreditFormDialog({
  shopId, editing, onClose, onDone,
}: {
  shopId: number;
  editing: CreditCode | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const xsFull = useXsFullScreen();
  const locked = !!editing && editing.used_count > 0; // after any redemption: only enabled + expiry
  const [form, setForm] = useState<FormState>(() => (editing ? {
    code: editing.code,
    kind: editing.kind,
    is_gift: editing.is_gift,
    percent_off: editing.percent_off?.toString() ?? "",
    amount_toman: editing.amount_toman?.toString() ?? "",
    max_bonus_toman: editing.max_bonus_toman?.toString() ?? "",
    min_topup_toman: editing.min_topup_toman ? String(editing.min_topup_toman) : "",
    max_uses: editing.max_uses?.toString() ?? "",
    per_customer_limit: editing.per_customer_limit?.toString() ?? "",
    expires_at: editing.expires_at ? editing.expires_at.slice(0, 16) : "",
    enabled: editing.enabled,
  } : emptyForm()));
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((p) => ({ ...p, [k]: v }));

  const createMut = useIdempotentMutation<Versioned<{ credit: CreditCode }>, CreditCreateBody>(
    (body, key) => createCreditCode(shopId, body, key),
    { onSuccess: () => onDone("کد ساخته شد.") },
  );
  const updateMut = useIdempotentMutation<Versioned<{ credit: CreditCode }>, CreditUpdateBody>(
    (body, key) => updateCreditCode(shopId, editing!.id, body, key),
    { onSuccess: () => onDone("کد به‌روزرسانی شد.") },
  );
  const busy = createMut.isPending || updateMut.isPending;
  const err = createMut.error ?? updateMut.error;

  const codeValid = form.code.trim().length >= 1 && form.code.trim().length <= 32;
  const valueValid = form.kind === "percent"
    ? num(form.percent_off) != null && Number(form.percent_off) >= 1 && Number(form.percent_off) <= 100
    : num(form.amount_toman) != null && Number(form.amount_toman) >= 1;
  const canSubmit = !busy && (locked || (codeValid && valueValid));

  const submit = () => {
    if (locked && editing) {
      updateMut.mutate({ enabled: form.enabled, expires_at: isoOrNull(form.expires_at) });
      return;
    }
    const base = {
      kind: form.kind,
      is_gift: form.kind === "fixed" ? form.is_gift : false,
      percent_off: form.kind === "percent" ? num(form.percent_off) : null,
      amount_toman: form.kind === "fixed" ? num(form.amount_toman) : null,
      max_bonus_toman: form.kind === "percent" ? num(form.max_bonus_toman) : null,
      min_topup_toman: num(form.min_topup_toman) ?? 0,
      max_uses: num(form.max_uses),
      per_customer_limit: num(form.per_customer_limit),
      expires_at: isoOrNull(form.expires_at),
    };
    if (editing) updateMut.mutate({ ...base, enabled: form.enabled });
    else createMut.mutate({ code: form.code.trim(), ...base } as CreditCreateBody);
  };

  return (
    <Dialog open onClose={() => !busy && onClose()} maxWidth="xs" fullWidth fullScreen={xsFull}>
      <DialogTitle>{editing ? "ویرایش کد" : "کد جدید"}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 0.5 }}>
          {locked && (
            <Alert severity="info">
              این کد قبلاً استفاده شده است؛ فقط وضعیت فعال/غیرفعال و تاریخ انقضا قابل تغییر است.
            </Alert>
          )}
          {err != null && !locked && (
            <Alert severity="error" sx={{ whiteSpace: "pre-line" }}>
              {storefrontErrorMessage(err, "ثبت ناموفق بود (ممکن است این کد از قبل وجود داشته باشد یا ورودی نامعتبر باشد).")}
            </Alert>
          )}
          {!editing && (
            <TextField label="کد" value={form.code} onChange={(e) => set("code", e.target.value)}
              inputProps={{ dir: "ltr", maxLength: 32 }} fullWidth
              error={!!form.code && !codeValid} helperText="۱ تا ۳۲ نویسه؛ برای مشتری به کوچک و بزرگیِ حروف حساس نیست" />
          )}
          {!locked && (
            <>
              <TextField select label="نوع" value={form.kind} onChange={(e) => set("kind", e.target.value as CreditKind)} fullWidth>
                <MenuItem value="percent">درصدی روی شارژ</MenuItem>
                <MenuItem value="fixed">مبلغ ثابت</MenuItem>
              </TextField>
              {form.kind === "percent" ? (
                <>
                  <NumberField label="درصد" value={form.percent_off}
                    onChange={(value) => set("percent_off", value)} fullWidth />
                  <NumberField label="سقفِ پاداش (تومان، اختیاری)" value={form.max_bonus_toman}
                    onChange={(value) => set("max_bonus_toman", value)} fullWidth />
                </>
              ) : (
                <>
                  <NumberField label="مبلغ (تومان)" value={form.amount_toman}
                    onChange={(value) => set("amount_toman", value)} fullWidth />
                  <FormControlLabel control={<Switch checked={form.is_gift}
                    onChange={(e) => set("is_gift", e.target.checked)} />}
                    label="هدیهٔ مستقیم (بدون نیاز به شارژ)" />
                </>
              )}
              {!form.is_gift && (
                <NumberField label="حداقل شارژ لازم (تومان، اختیاری)" value={form.min_topup_toman}
                  onChange={(value) => set("min_topup_toman", value)} fullWidth />
              )}
              <Stack direction="row" spacing={1.5}>
                <NumberField label="سقف کل استفاده" value={form.max_uses}
                  onChange={(value) => set("max_uses", value)} fullWidth />
                <NumberField label="سقف هر مشتری" value={form.per_customer_limit}
                  onChange={(value) => set("per_customer_limit", value)} fullWidth />
              </Stack>
            </>
          )}
          <TextField label="تاریخ انقضا (اختیاری)" type="datetime-local" value={form.expires_at}
            onChange={(e) => set("expires_at", e.target.value)} InputLabelProps={{ shrink: true }}
            inputProps={{ dir: "ltr" }} fullWidth />
          {editing && (
            <FormControlLabel control={<Switch checked={form.enabled}
              onChange={(e) => set("enabled", e.target.checked)} />} label="فعال" />
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>انصراف</Button>
        <Button variant="contained" onClick={submit} disabled={!canSubmit}>
          {busy ? "در حال ذخیره…" : "ذخیره"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function CreditUsageDialog({ shopId, code, onClose }: { shopId: number; code: CreditCode; onClose: () => void }) {
  const xsFull = useXsFullScreen();
  const usageQuery = useQuery({
    queryKey: storefrontQueryKeys.creditUsage(shopId, code.id),
    queryFn: () => getCreditUsage(shopId, code.id),
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const redQuery = useInfiniteQuery({
    queryKey: storefrontQueryKeys.creditRedemptions(shopId, code.id),
    queryFn: ({ pageParam }) => listCreditRedemptions(shopId, code.id, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const redemptions = useMemo(
    () => redQuery.data?.pages.flatMap((p) => p.items) ?? [], [redQuery.data]);
  const usage = usageQuery.data;

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth fullScreen={xsFull}>
      <DialogTitle>آمار کد <span dir="ltr">{code.code}</span></DialogTitle>
      <DialogContent dividers>
        <DataState isLoading={usageQuery.isLoading} isError={usageQuery.isError} rows={2}
          onRetry={() => usageQuery.refetch()}>
          <Stack spacing={1.5}>
            <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
              <Stat label="تعداد استفاده" value={fmtNum(usage?.total_redemptions ?? 0)} />
              <Stat label="مشتریان یکتا" value={fmtNum(usage?.unique_customers ?? 0)} />
              <Stat label="مجموعِ پاداش" value={fmtToman(usage?.total_bonus_toman ?? 0)} />
            </Stack>
            <Typography variant="subtitle2">آخرین استفاده‌ها</Typography>
            {!redemptions.length ? (
              <Typography variant="body2" color="text.disabled">هنوز استفاده‌ای ثبت نشده است.</Typography>
            ) : (
              <Stack spacing={0.75}>
                {redemptions.map((r) => (
                  <Stack key={r.id} direction="row" justifyContent="space-between">
                    <Typography variant="body2" dir="ltr">مشتری #{fmtNum(r.customer_id)}</Typography>
                    <Typography variant="body2" sx={{ fontWeight: 700 }}>{fmtToman(r.bonus_toman)}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {r.created_at ? fmtDateTime(r.created_at) : "—"}
                    </Typography>
                  </Stack>
                ))}
                {redQuery.hasNextPage && (
                  <Button size="small" onClick={() => redQuery.fetchNextPage()} disabled={redQuery.isFetchingNextPage}>
                    نمایش بیشتر
                  </Button>
                )}
              </Stack>
            )}
          </Stack>
        </DataState>
      </DialogContent>
      <DialogActions>
        <Button variant="contained" onClick={onClose}>بستن</Button>
      </DialogActions>
    </Dialog>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ minWidth: 120 }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="h6">{value}</Typography>
    </Box>
  );
}
