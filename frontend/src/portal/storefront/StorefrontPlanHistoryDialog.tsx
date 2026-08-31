import { Button, Dialog, DialogActions, DialogContent, DialogTitle, List, ListItem, ListItemText, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { DataState } from "../../components/DataState";
import { fmtDateTime, fmtNum, fmtToman } from "../../format";
import { getStorefrontPlanHistory, storefrontQueryKeys } from "./api";
import { planLabel } from "./planLabel";
import type { StorefrontPlan } from "./types";
import { useXsFullScreen } from "../../responsive";

export default function StorefrontPlanHistoryDialog({
  shopId,
  plan,
  onClose,
}: {
  shopId: number;
  plan: StorefrontPlan | null;
  onClose: () => void;
}) {
  const xsFull = useXsFullScreen();
  const query = useQuery({
    queryKey: storefrontQueryKeys.planHistory(shopId, plan?.id || 0),
    queryFn: () => getStorefrontPlanHistory(shopId, plan!.id),
    enabled: !!plan,
  });

  return (
    <Dialog open={!!plan} onClose={onClose} maxWidth="sm" fullWidth fullScreen={xsFull}>
      <DialogTitle>تاریخچهٔ پلن {plan ? planLabel(plan) : ""}</DialogTitle>
      <DialogContent>
        <DataState isLoading={query.isLoading} isError={query.isError} error={query.error} rows={4} onRetry={() => query.refetch()}>
          {!query.data?.length ? (
            <Typography color="text.secondary" variant="body2" sx={{ py: 3, textAlign: "center" }}>
              تغییری برای این پلن ثبت نشده است.
            </Typography>
          ) : (
            <List disablePadding>
              {query.data.map((event) => (
                <ListItem key={event.id} divider alignItems="flex-start">
                  <ListItemText
                    primary={ACTION_FA[event.action] || event.action}
                    secondary={
                      <Stack component="span" spacing={0.4} sx={{ mt: 0.5 }}>
                        <span>
                          {SOURCE_FA[event.source] || event.source}
                          {" · "}{ROLE_FA[event.actor_role] || event.actor_role}
                          {" · "}{fmtDateTime(event.created_at)}
                          {event.outcome && event.outcome !== "succeeded"
                            ? ` · ${OUTCOME_FA[event.outcome] || event.outcome}`
                            : ""}
                        </span>
                        {event.before && <span>قبل: {planSnapshot(event.before)}</span>}
                        {event.after && <span>بعد: {planSnapshot(event.after)}</span>}
                      </Stack>
                    }
                  />
                </ListItem>
              ))}
            </List>
          )}
        </DataState>
      </DialogContent>
      <DialogActions><Button onClick={onClose}>بستن</Button></DialogActions>
    </Dialog>
  );
}

// The audit log stores machine tokens (`plan.create`, `portal`, `owner`). Rendering them raw put
// LTR identifiers in the middle of a Persian RTL list and told a shop owner nothing.
const ACTION_FA: Record<string, string> = {
  "plan.create": "ساخت پلن",
  "plan.update": "ویرایش پلن",
  "plan.set_enabled": "فعال/غیرفعال کردن",
  "plan.delete": "حذف پلن",
  "plan.reorder": "تغییر ترتیب",
};
const SOURCE_FA: Record<string, string> = { portal: "پنل", bot: "ربات", system: "سامانه" };
const ROLE_FA: Record<string, string> = { owner: "مالک", manager: "مدیر", admin: "مدیر" };
// Every outcome `_execute`/`_claim_db_command` (backend/app/services/storefront_admin.py) can
// actually write, besides "succeeded" (never shown — see the filter above this table).
const OUTCOME_FA: Record<string, string> = {
  failed: "ناموفق",
  conflict: "درگیری هم‌زمان",
  started: "شروع‌شده",
  unknown: "نامشخص",
};

function planSnapshot(value: Partial<StorefrontPlan>) {
  const parts = [];
  if (value.title !== undefined) parts.push(value.title || "بدون نام");
  if (value.gb !== undefined) parts.push(`${fmtNum(value.gb)} گیگابایت`);
  if (value.days !== undefined) parts.push(`${fmtNum(value.days)} روز`);
  if (value.price_toman !== undefined) parts.push(fmtToman(value.price_toman));
  if (value.enabled !== undefined) parts.push(value.enabled ? "فعال" : "غیرفعال");
  return parts.join(" · ") || "—";
}
