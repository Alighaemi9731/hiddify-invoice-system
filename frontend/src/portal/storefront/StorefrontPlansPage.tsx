import { FormEvent, useEffect, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, IconButton, Stack, Switch, TextField, Tooltip, Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/esm/Add";
import ArrowDownwardIcon from "@mui/icons-material/esm/ArrowDownward";
import ArrowUpwardIcon from "@mui/icons-material/esm/ArrowUpward";
import DeleteOutlineIcon from "@mui/icons-material/esm/DeleteOutline";
import DragIndicatorIcon from "@mui/icons-material/esm/DragIndicator";
import EditIcon from "@mui/icons-material/esm/Edit";
import HistoryIcon from "@mui/icons-material/esm/History";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { NumberField, numberValue } from "../../components/NumberField";
import { fmtNum, fmtToman } from "../../format";
import {
  createStorefrontPlan, deleteStorefrontPlan, listStorefrontPlans, reorderStorefrontPlans,
  setStorefrontPlanEnabled, storefrontQueryKeys, updateStorefrontPlan,
} from "./api";
import StorefrontConflictDialog from "./StorefrontConflictDialog";
import StorefrontPlanHistoryDialog from "./StorefrontPlanHistoryDialog";
import type { StorefrontOutletContext } from "./StorefrontShell";
import { planLabel } from "./planLabel";
import type { StorefrontPlan, StorefrontPlanDraft, Versioned } from "./types";
import { isVersionConflict, storefrontErrorMessage, useIdempotentMutation } from "./mutation";
import { useXsFullScreen } from "../../responsive";

type PlanCommand =
  | { type: "create"; draft: StorefrontPlanDraft; etag?: string }
  | { type: "update"; planId: number; draft: Partial<StorefrontPlanDraft>; etag?: string }
  | { type: "enabled"; planId: number; enabled: boolean; etag?: string }
  | { type: "delete"; planId: number; etag?: string }
  | { type: "reorder"; planIds: number[]; etag?: string };

// The three NUMERIC fields hold TEXT, not numbers: a field being emptied mid-edit is a legal
// intermediate state, and `Number("")` is 0 (see components/NumberField). Parsing happens once,
// below. `title` is genuinely text — optional, and `""` means "unnamed".
type PlanForm = { title: string; gb: string; days: string; price_toman: string };
const EMPTY_FORM: PlanForm = { title: "", gb: "", days: "30", price_toman: "" };
const TITLE_MAX = 64;

export default function StorefrontPlansPage() {
  const xsFull = useXsFullScreen();
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();
  const queryKey = storefrontQueryKeys.plans(shop.id);
  const plansQuery = useQuery({ queryKey, queryFn: () => listStorefrontPlans(shop.id) });
  const [orderedPlans, setOrderedPlans] = useState<StorefrontPlan[]>([]);
  const [draggedId, setDraggedId] = useState<number | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<StorefrontPlan | null>(null);
  const [form, setForm] = useState<PlanForm>(EMPTY_FORM);
  const [historyPlan, setHistoryPlan] = useState<StorefrontPlan | null>(null);
  const [conflict, setConflict] = useState<PlanCommand | null>(null);
  const [conflictReloadError, setConflictReloadError] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => setOrderedPlans(plansQuery.data?.data || []), [plansQuery.data]);

  const command = useIdempotentMutation<Versioned<unknown>, PlanCommand>(
    async (input, key) => {
      const etag = input.etag || plansQuery.data?.etag || "";
      if (!etag) throw new Error("missing storefront config version");
      if (input.type === "create") return createStorefrontPlan(shop.id, input.draft, etag, key);
      if (input.type === "update") return updateStorefrontPlan(shop.id, input.planId, input.draft, etag, key);
      if (input.type === "enabled") return setStorefrontPlanEnabled(shop.id, input.planId, input.enabled, etag, key);
      if (input.type === "delete") return deleteStorefrontPlan(shop.id, input.planId, etag, key);
      return reorderStorefrontPlans(shop.id, input.planIds, etag, key);
    },
    {
      // One lane per command TYPE: nudging a plan up while a toggle is still in flight is a normal
      // thing to do, and used to be rejected outright (silently, as a "connection" error).
      commandKey: (input) => input.type,
      onSuccess: async (result) => {
        if (result.etag) {
          queryClient.setQueryData<Versioned<StorefrontPlan[]>>(queryKey, (current) =>
            current ? { ...current, etag: result.etag } : current);
        }
        await Promise.all([
          plansQuery.refetch(),
          queryClient.invalidateQueries({ queryKey: storefrontQueryKeys.preview(shop.id), exact: true }),
        ]);
        setMessage("تغییرات ذخیره شد.");
        setFormOpen(false);
        setEditing(null);
      },
      onError: (error, variables) => {
        if (isVersionConflict(error)) setConflict(variables);
      },
    },
  );

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormOpen(true);
  };
  const openEdit = (plan: StorefrontPlan) => {
    setEditing(plan);
    setForm({
      title: plan.title || "", gb: String(plan.gb), days: String(plan.days),
      price_toman: String(plan.price_toman),
    });
    setFormOpen(true);
  };

  const gb = numberValue(form.gb);
  const days = numberValue(form.days);
  const price = numberValue(form.price_toman);
  // A plan may never be sold below what its quota costs this reseller. The server is authoritative
  // (see storefront_pricing); mirroring the floor here just avoids a pointless 422 round-trip.
  const costPerGb = shop.cost_per_gb_toman || 0;
  const floorFor = (quota: number) => costPerGb * Math.max(0, quota || 0);
  const draftFloor = gb === null ? 0 : floorFor(gb);
  const belowCost = (plan: { gb: number; price_toman: number }) =>
    floorFor(plan.gb) > 0 && plan.price_toman < floorFor(plan.gb);
  const priceUnderFloor = draftFloor > 0 && price !== null && price < draftFloor;
  const underpricedPlans = orderedPlans.filter(belowCost);
  const gbValid = gb !== null && Number.isInteger(gb) && gb >= 1 && gb <= 100_000;
  const daysValid = days !== null && Number.isInteger(days) && days >= 1 && days <= 3650;
  const priceValid = price !== null && Number.isInteger(price)
    && price >= 0 && price <= 1_000_000_000_000;
  const title = form.title.trim().replace(/\s+/g, " ");
  const titleValid = title.length <= TITLE_MAX;
  const valid = gbValid && daysValid && priceValid && titleValid && !priceUnderFloor;
  // Clearing a name is a real change: `title` goes to `""`, which the server accepts as "unnamed".
  // (It refuses `null` — that would be indistinguishable from "field not sent".)
  const planChanges: Partial<StorefrontPlanDraft> = !valid ? {} : changedFields(
    editing && {
      title: editing.title || "", gb: editing.gb, days: editing.days,
      price_toman: editing.price_toman,
    },
    { title, gb: gb!, days: days!, price_toman: price! },
  );
  const hasPlanChanges = Object.keys(planChanges).length > 0;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!valid || !hasPlanChanges || command.isPending) return;
    command.mutate(editing
      ? { type: "update", planId: editing.id, draft: planChanges }
      : { type: "create", draft: { title, gb: gb!, days: days!, price_toman: price! } });
  };

  const reorder = (plans: StorefrontPlan[]) => {
    setOrderedPlans(plans);
    command.mutate({ type: "reorder", planIds: plans.map((plan) => plan.id) });
  };
  const move = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= orderedPlans.length || command.isPending) return;
    const next = [...orderedPlans];
    [next[index], next[target]] = [next[target], next[index]];
    reorder(next);
  };
  const dropOn = (targetId: number) => {
    if (draggedId == null || draggedId === targetId || command.isPending) return;
    const next = [...orderedPlans];
    const from = next.findIndex((plan) => plan.id === draggedId);
    const to = next.findIndex((plan) => plan.id === targetId);
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    setDraggedId(null);
    reorder(next);
  };

  return (
    <Box>
      <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" spacing={1.5} sx={{ mb: 2 }}>
        <Box>
          <Typography variant="h6">پلن‌های فروش</Typography>
          <Typography variant="body2" color="text.secondary">پلن‌ها را بسازید، مرتب کنید یا موقتاً غیرفعال کنید.</Typography>
        </Box>
        <Button variant="contained" startIcon={<AddIcon />} onClick={openCreate}>پلن جدید</Button>
      </Stack>
      {message && <Alert severity="success" onClose={() => setMessage(null)} sx={{ mb: 2 }}>{message}</Alert>}
      {command.isError && !isVersionConflict(command.error) && (
        <Alert severity="error" sx={{ mb: 2, whiteSpace: "pre-line" }}>
          {storefrontErrorMessage(command.error, "ذخیرهٔ تغییرات انجام نشد؛ دوباره تلاش کنید.")}
        </Alert>
      )}
      {underpricedPlans.length > 0 && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          قیمتِ {fmtNum(underpricedPlans.length)} پلن از هزینهٔ خودتان کمتر است و فروششان برای شما ضرر
          دارد. قیمت به تومان است، نه هزار تومان — برای ۵۰ هزار تومان باید بنویسید 50000.
        </Alert>
      )}
      <DataState isLoading={plansQuery.isLoading} isError={plansQuery.isError} error={plansQuery.error} rows={5} onRetry={() => plansQuery.refetch()}>
        {!orderedPlans.length ? (
          <Alert severity="info">هنوز پلنی ساخته نشده است.</Alert>
        ) : (
          <Stack spacing={1.25}>
            {orderedPlans.map((plan, index) => (
              <Card
                key={plan.id}
                draggable={!command.isPending}
                onDragStart={() => setDraggedId(plan.id)}
                onDragEnd={() => setDraggedId(null)}
                onDragOver={(event) => event.preventDefault()}
                onDrop={() => dropOn(plan.id)}
                sx={{ opacity: draggedId === plan.id ? 0.55 : 1 }}
              >
                <CardContent sx={{ "&:last-child": { pb: 2 } }}>
                  <Stack direction={{ xs: "column", sm: "row" }} alignItems={{ xs: "stretch", sm: "center" }} spacing={1.5}>
                    <Stack direction="row" alignItems="center" sx={{ flexGrow: 1, minWidth: 0 }} spacing={1}>
                      <DragIndicatorIcon color="disabled" aria-hidden />
                      <Box sx={{ minWidth: 0 }}>
                        {/* Identical wording to the bot's own plan button (`keyboards.plan_label`),
                            so a reseller sees ONE description of a plan on both surfaces. */}
                        <Typography sx={{ fontWeight: 800 }}>{planLabel(plan)}</Typography>
                        <Typography variant="body2" color="text.secondary">
                          {fmtToman(plan.price_toman)}
                        </Typography>
                      </Box>
                    </Stack>
                    <Stack direction="row" alignItems="center" justifyContent={{ xs: "space-between", sm: "flex-end" }}>
                      <Tooltip title={!plan.enabled && belowCost(plan) ? `قیمت این پلن از هزینهٔ خودتان (${fmtToman(floorFor(plan.gb))}) کمتر است؛ ابتدا قیمت را اصلاح کنید.` : ""}>
                        <FormControlLabel
                          control={<Switch checked={plan.enabled} onChange={(_, enabled) => command.mutate({ type: "enabled", planId: plan.id, enabled })} disabled={command.isPending || (!plan.enabled && belowCost(plan))} />}
                          label={plan.enabled ? "فعال" : "غیرفعال"}
                        />
                      </Tooltip>
                      <Tooltip title="انتقال به بالا"><span><IconButton aria-label={`انتقال ${planLabel(plan)} به بالا`} disabled={index === 0 || command.isPending} onClick={() => move(index, -1)}><ArrowUpwardIcon /></IconButton></span></Tooltip>
                      <Tooltip title="انتقال به پایین"><span><IconButton aria-label={`انتقال ${planLabel(plan)} به پایین`} disabled={index === orderedPlans.length - 1 || command.isPending} onClick={() => move(index, 1)}><ArrowDownwardIcon /></IconButton></span></Tooltip>
                      <IconButton aria-label="تاریخچه پلن" onClick={() => setHistoryPlan(plan)}><HistoryIcon /></IconButton>
                      <IconButton aria-label="ویرایش پلن" onClick={() => openEdit(plan)}><EditIcon /></IconButton>
                      <IconButton
                        aria-label="حذف پلن"
                        color="error"
                        disabled={command.isPending}
                        onClick={() => window.confirm("این پلن حذف شود؟ تاریخچهٔ سفارش‌ها حفظ خواهد شد.") && command.mutate({ type: "delete", planId: plan.id })}
                      ><DeleteOutlineIcon /></IconButton>
                    </Stack>
                  </Stack>
                </CardContent>
              </Card>
            ))}
          </Stack>
        )}
      </DataState>

      <Dialog open={formOpen} onClose={() => !command.isPending && setFormOpen(false)} maxWidth="xs" fullWidth fullScreen={xsFull}>
        <Box component="form" onSubmit={submit}>
          <DialogTitle>{editing ? "ویرایش پلن" : "ساخت پلن"}</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ pt: 1 }}>
              {/* Optional, and a plain TextField on purpose: NumberField exists for the RTL caret
                  and empty-value problems of numeric inputs, none of which apply to a name. */}
              <TextField
                autoFocus
                label="نام پلن (اختیاری)"
                value={form.title}
                error={!titleValid}
                helperText={`برای بی‌نام‌ماندن خالی بگذارید — حداکثر ${fmtNum(TITLE_MAX)} نویسه`}
                inputProps={{ maxLength: TITLE_MAX }}
                onChange={(event) => setForm({ ...form, title: event.target.value })}
              />
              <NumberField
                required
                label="حجم (گیگابایت)"
                value={form.gb}
                error={form.gb !== "" && !gbValid}
                helperText="عددی بین ۱ تا ۱۰۰۰۰۰"
                onChange={(value) => setForm({ ...form, gb: value })}
              />
              <NumberField
                required
                label="مدت (روز)"
                value={form.days}
                error={form.days !== "" && !daysValid}
                helperText="عددی بین ۱ تا ۳۶۵۰"
                onChange={(value) => setForm({ ...form, days: value })}
              />
              <NumberField
                required
                label="قیمت (تومان)"
                value={form.price_toman}
                error={(form.price_toman !== "" && !priceValid) || priceUnderFloor}
                helperText={
                  draftFloor > 0
                    ? `قیمت به تومان است، نه هزار تومان (برای ۵۰ هزار تومان بنویسید 50000). کفِ مجاز برای این پلن: ${fmtToman(draftFloor)} — هزینهٔ هر گیگابایت برای شما ${fmtToman(costPerGb)}`
                    : "قیمت به تومان است، نه هزار تومان (برای ۵۰ هزار تومان بنویسید 50000)."
                }
                onChange={(value) => setForm({ ...form, price_toman: value })}
              />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setFormOpen(false)} disabled={command.isPending}>انصراف</Button>
            <Button type="submit" variant="contained" disabled={!valid || !hasPlanChanges || command.isPending}>ذخیره</Button>
          </DialogActions>
        </Box>
      </Dialog>

      <StorefrontPlanHistoryDialog shopId={shop.id} plan={historyPlan} onClose={() => setHistoryPlan(null)} />
      <StorefrontConflictDialog
        open={!!conflict}
        busy={plansQuery.isFetching || command.isPending}
        reloadError={conflictReloadError}
        onClose={() => { setConflict(null); setConflictReloadError(false); }}
        onReload={() => void plansQuery.refetch().then((fresh) => {
          if (fresh.isSuccess) {
            setConflict(null);
            setConflictReloadError(false);
            setFormOpen(false);
            setEditing(null);
            setForm(EMPTY_FORM);
            setOrderedPlans(fresh.data?.data || []);
          }
          else setConflictReloadError(true);
        })}
        onReapply={() => void plansQuery.refetch().then((fresh) => {
          if (conflict && fresh.isSuccess && fresh.data?.etag) {
            command.mutate({ ...conflict, etag: fresh.data.etag });
            setConflict(null);
            setConflictReloadError(false);
          } else setConflictReloadError(true);
        })}
      />
    </Box>
  );
}

/** Fields that actually differ from the stored plan; every field when creating (`before` null). */
function changedFields<T extends object>(before: T | null | undefined, after: T): Partial<T> {
  if (!before) return after;
  return Object.fromEntries(
    (Object.keys(after) as Array<keyof T>)
      .filter((key) => before[key] !== after[key])
      .map((key) => [key, after[key]]),
  ) as Partial<T>;
}
