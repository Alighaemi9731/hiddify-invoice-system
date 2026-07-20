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
import { fmtNum, fmtToman } from "../../format";
import {
  createStorefrontPlan, deleteStorefrontPlan, listStorefrontPlans, reorderStorefrontPlans,
  setStorefrontPlanEnabled, storefrontQueryKeys, updateStorefrontPlan,
} from "./api";
import StorefrontConflictDialog from "./StorefrontConflictDialog";
import StorefrontPlanHistoryDialog from "./StorefrontPlanHistoryDialog";
import type { StorefrontOutletContext } from "./StorefrontShell";
import type { StorefrontPlan, StorefrontPlanDraft, Versioned } from "./types";
import { commandRecoveryMessage, isVersionConflict, useIdempotentMutation } from "./mutation";

type PlanCommand =
  | { type: "create"; draft: StorefrontPlanDraft; etag?: string }
  | { type: "update"; planId: number; draft: Partial<StorefrontPlanDraft>; etag?: string }
  | { type: "enabled"; planId: number; enabled: boolean; etag?: string }
  | { type: "delete"; planId: number; etag?: string }
  | { type: "reorder"; planIds: number[]; etag?: string };

const EMPTY_DRAFT: StorefrontPlanDraft = { title: "", gb: 1, days: 30, price_toman: 0 };

export default function StorefrontPlansPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const queryClient = useQueryClient();
  const queryKey = storefrontQueryKeys.plans(shop.id);
  const plansQuery = useQuery({ queryKey, queryFn: () => listStorefrontPlans(shop.id) });
  const [orderedPlans, setOrderedPlans] = useState<StorefrontPlan[]>([]);
  const [draggedId, setDraggedId] = useState<number | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<StorefrontPlan | null>(null);
  const [draft, setDraft] = useState<StorefrontPlanDraft>(EMPTY_DRAFT);
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
    setDraft(EMPTY_DRAFT);
    setFormOpen(true);
  };
  const openEdit = (plan: StorefrontPlan) => {
    setEditing(plan);
    setDraft({ title: plan.title, gb: plan.gb, days: plan.days, price_toman: plan.price_toman });
    setFormOpen(true);
  };
  const valid = draft.gb >= 1 && draft.gb <= 100_000
    && draft.days >= 1 && draft.days <= 3650
    && draft.price_toman >= 0 && draft.price_toman <= 1_000_000_000_000
    && (draft.title?.length || 0) <= 128;
  const planChanges = editing ? changedFields<StorefrontPlanDraft>(
    { title: editing.title, gb: editing.gb, days: editing.days, price_toman: editing.price_toman },
    draft,
  ) : draft;
  const hasPlanChanges = !editing || Object.keys(planChanges).length > 0;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!valid || command.isPending) return;
    command.mutate(editing
      ? { type: "update", planId: editing.id, draft: planChanges }
      : { type: "create", draft: { ...draft, title: draft.title?.trim() || undefined } });
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
        <Alert severity="error" sx={{ mb: 2 }}>
          {commandRecoveryMessage(command.error) || "ذخیرهٔ تغییرات انجام نشد. ورودی‌ها و اتصال را بررسی کنید."}
        </Alert>
      )}
      <DataState isLoading={plansQuery.isLoading} isError={plansQuery.isError} rows={5} onRetry={() => plansQuery.refetch()}>
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
                        <Typography sx={{ fontWeight: 800 }}>{plan.title || `پلن ${fmtNum(index + 1)}`}</Typography>
                        <Typography variant="body2" color="text.secondary">
                          {fmtNum(plan.gb)} گیگابایت · {fmtNum(plan.days)} روز · {fmtToman(plan.price_toman)}
                        </Typography>
                      </Box>
                    </Stack>
                    <Stack direction="row" alignItems="center" justifyContent={{ xs: "space-between", sm: "flex-end" }}>
                      <FormControlLabel
                        control={<Switch checked={plan.enabled} onChange={(_, enabled) => command.mutate({ type: "enabled", planId: plan.id, enabled })} disabled={command.isPending} />}
                        label={plan.enabled ? "فعال" : "غیرفعال"}
                      />
                      <Tooltip title="انتقال به بالا"><span><IconButton aria-label={`انتقال ${plan.title || plan.id} به بالا`} disabled={index === 0 || command.isPending} onClick={() => move(index, -1)}><ArrowUpwardIcon /></IconButton></span></Tooltip>
                      <Tooltip title="انتقال به پایین"><span><IconButton aria-label={`انتقال ${plan.title || plan.id} به پایین`} disabled={index === orderedPlans.length - 1 || command.isPending} onClick={() => move(index, 1)}><ArrowDownwardIcon /></IconButton></span></Tooltip>
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

      <Dialog open={formOpen} onClose={() => !command.isPending && setFormOpen(false)} maxWidth="xs" fullWidth>
        <Box component="form" onSubmit={submit}>
          <DialogTitle>{editing ? "ویرایش پلن" : "ساخت پلن"}</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ pt: 1 }}>
              <TextField label="عنوان (اختیاری)" value={draft.title || ""} inputProps={{ maxLength: 128 }} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
              <TextField required label="حجم (گیگابایت)" type="number" value={draft.gb} inputProps={{ min: 1, max: 100000 }} error={draft.gb < 1 || draft.gb > 100000} onChange={(event) => setDraft({ ...draft, gb: Number(event.target.value) })} />
              <TextField required label="مدت (روز)" type="number" value={draft.days} inputProps={{ min: 1, max: 3650 }} error={draft.days < 1 || draft.days > 3650} onChange={(event) => setDraft({ ...draft, days: Number(event.target.value) })} />
              <TextField required label="قیمت (تومان)" type="number" value={draft.price_toman} inputProps={{ min: 0, max: 1000000000000 }} error={draft.price_toman < 0 || draft.price_toman > 1_000_000_000_000} onChange={(event) => setDraft({ ...draft, price_toman: Number(event.target.value) })} />
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
            setDraft(EMPTY_DRAFT);
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

function changedFields<T extends object>(before: T, after: T): Partial<T> {
  return Object.fromEntries(
    (Object.keys(after) as Array<keyof T>)
      .filter((key) => before[key] !== after[key])
      .map((key) => [key, after[key]]),
  ) as Partial<T>;
}
