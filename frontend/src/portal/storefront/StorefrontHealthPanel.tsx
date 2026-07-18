import { Alert, Chip, Stack, Typography } from "@mui/material";
import CheckCircleOutlineIcon from "@mui/icons-material/esm/CheckCircleOutline";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import { fmtNum } from "../../format";
import { SectionCard } from "../ui";
import { getStorefrontHealth, storefrontQueryKeys } from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";

const ERROR_LABELS: Record<string, string> = {
  unauthorized: "توکن ربات معتبر نیست — ربات را دوباره راه‌اندازی کنید",
  network: "ارتباط با سرویس برقرار نیست — معمولاً خودش برطرف می‌شود",
  configuration: "تنظیمات فروشگاه کامل نیست",
  unknown: "یک خطا ثبت شده است",
};

// Only the states a RESELLER can act on. Panel/infrastructure internals (panel enabled, panel
// status enum, last sync time) were removed: they are the owner's servers, the reseller can do
// nothing about them, and they were the source of the mismatched labels — raw backend values like
// "active"/"ok" leaked into a Persian UI next to translated «فعال»/«غیرفعال» chips. Provider trouble
// is now surfaced as ONE plain roll-up line instead.
export default function StorefrontHealthPanel() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const query = useQuery({
    queryKey: storefrontQueryKeys.health(shop.id),
    queryFn: () => getStorefrontHealth(shop.id),
  });
  const health = query.data;

  const botLive = !!health?.bot.enabled && health?.bot.status === "active" && !health?.bot.error_class;
  const serviceOk = !!health?.panel.enabled && health?.panel.status === "ok" && !health?.panel.error_class;
  const open = !shop.shop_closed;
  const healthy = botLive && serviceOk && open;
  const errorClass = health?.bot.error_class || health?.panel.error_class;
  const failedOps = Number(health?.operation_states?.failed || 0);

  return (
    <DataState isLoading={query.isLoading} isError={query.isError} rows={3} onRetry={() => query.refetch()}>
      {health && (
        <Stack spacing={2}>
          {healthy ? (
            <Alert severity="success" icon={<CheckCircleOutlineIcon />}>
              فروشگاه شما سالم است و سفارش‌ها به‌درستی انجام می‌شوند.
            </Alert>
          ) : (
            <Alert severity="warning" icon={<WarningAmberIcon />}>
              {!open
                ? "فروشگاه شما بسته است؛ مشتری‌ها نمی‌توانند خرید کنند. از «تنظیمات» می‌توانید بازش کنید."
                : !botLive
                  ? (errorClass ? ERROR_LABELS[errorClass] : "ربات فروشگاه فعال نیست.")
                  : "اختلالی در سرویس‌دهی وجود دارد؛ در حال بررسی است. معمولاً خودش برطرف می‌شود."}
            </Alert>
          )}

          <SectionCard title="وضعیت">
            <Stack spacing={1.4}>
              <HealthRow label="ربات فروشگاه" ok={botLive} okText="فعال" badText="غیرفعال" />
              <HealthRow label="فروشگاه" ok={open} okText="باز" badText="بسته" />
              <HealthRow label="سرویس‌دهی" ok={serviceOk} okText="سالم" badText="اختلال" />
              {failedOps > 0 && (
                <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
                  <Typography variant="body2" color="text.secondary">سفارش‌های ناموفق</Typography>
                  <Chip size="small" color="error" label={fmtNum(failedOps)} />
                </Stack>
              )}
            </Stack>
          </SectionCard>
        </Stack>
      )}
    </DataState>
  );
}

// One rendering path for every row — the old component had two (a translated boolean chip and a
// raw-string chip), which is why the labels disagreed and error states showed as neutral grey.
function HealthRow({
  label, ok, okText, badText,
}: { label: string; ok: boolean; okText: string; badText: string }) {
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Chip size="small" color={ok ? "success" : "warning"} label={ok ? okText : badText} />
    </Stack>
  );
}
