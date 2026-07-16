import { Alert, Chip, Grid, Stack, Typography } from "@mui/material";
import CheckCircleOutlineIcon from "@mui/icons-material/esm/CheckCircleOutline";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtDateTime, fmtNum } from "../../format";
import { SectionCard } from "../ui";
import { getStorefrontHealth, storefrontQueryKeys } from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";

const OPERATION_LABELS: Record<string, string> = {
  pending: "در انتظار",
  in_progress: "در حال اجرا",
  done: "موفق",
  failed: "ناموفق",
  reversed: "برگشت‌خورده",
};

const ERROR_LABELS: Record<string, string> = {
  unauthorized: "مجوز ربات یا پنل معتبر نیست",
  network: "ارتباط با سرویس بیرونی برقرار نیست",
  configuration: "تنظیمات فروشگاه کامل نیست",
  unknown: "یک خطای ناشناخته ثبت شده است",
};

export default function StorefrontHealthPanel() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const query = useQuery({
    queryKey: storefrontQueryKeys.health(shop.id),
    queryFn: () => getStorefrontHealth(shop.id),
  });
  const health = query.data;
  const healthy = !!health
    && health.bot.enabled
    && health.bot.status === "active"
    && health.panel.enabled
    && health.panel.status === "ok"
    && !shop.shop_closed
    && !health.bot.error_class
    && !health.panel.error_class;
  const errorClass = health?.bot.error_class || health?.panel.error_class;
  const latestStateUpdate = [health?.bot.state_updated_at, health?.panel.state_updated_at]
    .filter((value): value is string => !!value)
    .sort()
    .pop() || null;
  const stateUpdatedAt = health?.bot.error_class
    ? health.bot.state_updated_at
    : health?.panel.error_class
      ? health.panel.state_updated_at
      : latestStateUpdate;

  return (
    <DataState
      isLoading={query.isLoading}
      isError={query.isError}
      rows={4}
      onRetry={() => query.refetch()}
    >
      {query.data && (
        <Stack spacing={2}>
          {errorClass && (
            <Alert severity="warning" icon={<WarningAmberIcon />}>
              {ERROR_LABELS[errorClass]}
              {stateUpdatedAt
                ? ` — آخرین به‌روزرسانی وضعیت: ${fmtDateTime(stateUpdatedAt)}`
                : ""}
            </Alert>
          )}
          {!errorClass && !healthy && (
            <Alert severity="warning" icon={<WarningAmberIcon />}>
              وضعیت ثبت‌شدهٔ فروشگاه عادی نیست. وضعیت ربات، پنل و باز بودن فروشگاه را بررسی کنید.
              {stateUpdatedAt ? ` — آخرین به‌روزرسانی وضعیت: ${fmtDateTime(stateUpdatedAt)}` : ""}
            </Alert>
          )}
          {healthy && (
            <Alert severity="success" icon={<CheckCircleOutlineIcon />}>
              وضعیت ثبت‌شدهٔ فروشگاه عادی است.
            </Alert>
          )}

          <Grid container spacing={2}>
            <Grid item xs={12} md={5}>
              <SectionCard title="وضعیت اجزا">
                <Stack spacing={1.4}>
                  <HealthRow label="فعال بودن ربات" ok={query.data.bot.enabled} />
                  <HealthRow label="باز بودن فروشگاه" ok={!shop.shop_closed} />
                  <HealthRow label="وضعیت ربات" value={query.data.bot.status} />
                  <HealthRow label="وضعیت پنل" value={query.data.panel.status} />
                  <HealthRow label="فعال بودن پنل" ok={query.data.panel.enabled} />
                  <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
                    <Typography variant="body2" color="text.secondary">آخرین همگام‌سازی پنل</Typography>
                    <Typography variant="caption">{fmtDateTime(query.data.panel.last_synced_at)}</Typography>
                  </Stack>
                </Stack>
              </SectionCard>
            </Grid>
            <Grid item xs={12} md={7}>
              <SectionCard title="عملیات‌های ثبت‌شده">
                <Stack direction="row" gap={1} flexWrap="wrap">
                  {Object.entries(query.data.operation_states).map(([state, count]) => (
                    <Chip
                      key={state}
                      variant="outlined"
                      color={state === "failed" ? "error" : state === "done" ? "success" : "default"}
                      label={`${OPERATION_LABELS[state] || state}: ${fmtNum(count)}`}
                    />
                  ))}
                </Stack>
              </SectionCard>
            </Grid>
          </Grid>
        </Stack>
      )}
    </DataState>
  );
}

function HealthRow({ label, ok, value }: { label: string; ok?: boolean; value?: string }) {
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Chip
        size="small"
        color={ok === undefined ? "default" : ok ? "success" : "warning"}
        label={value || (ok ? "فعال" : "غیرفعال")}
      />
    </Stack>
  );
}
