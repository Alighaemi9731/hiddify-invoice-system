import { Box, Chip, Grid, LinearProgress, Stack, Typography } from "@mui/material";
import ShoppingCartIcon from "@mui/icons-material/esm/ShoppingCart";
import GroupIcon from "@mui/icons-material/esm/Group";
import AutorenewIcon from "@mui/icons-material/esm/Autorenew";
import { useQuery } from "@tanstack/react-query";
import { useTheme } from "@mui/material/styles";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import StatCard from "../../components/StatCard";
import EChart from "../../components/EChart";
import { dailyTrendOption } from "../dailyTrend";
import { fmtNum, fmtToman } from "../../format";
import { SectionCard } from "../ui";
import { currentTehranMonthRange, getStorefrontDashboard, storefrontQueryKeys } from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";

const SERVICE_LABELS: Record<string, string> = {
  provisioned: "فعال",
  renewing: "در حال تمدید",
  pending: "در انتظار",
  disabled: "غیرفعال",
  failed: "ناموفق",
  deleted: "حذف‌شده",
};

// Rebuilt around questions a shop owner actually asks: how much did I sell (and is that better or
// worse than last month), which days sell, which plans sell, and how many services are about to
// lapse. The old page's only chart re-drew the same three numbers printed directly beneath it
// («تفکیک فروش دوره»), which told nobody anything.
export default function StorefrontDashboardPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const theme = useTheme();
  const { from, to } = currentTehranMonthRange();
  const query = useQuery({
    queryKey: storefrontQueryKeys.dashboard(shop.id, from, to),
    queryFn: () => getStorefrontDashboard(shop.id, from, to),
  });
  const d = query.data;

  const month = d?.sales_month.net_sales_toman ?? 0;
  const prev = d?.sales_prev_month?.net_sales_toman ?? null;
  // Month-over-month, so the headline number has context instead of standing alone.
  const delta = prev && prev > 0 ? Math.round(((month - prev) / prev) * 100) : null;
  const monthSub = prev === null
    ? undefined
    : delta === null
      ? `ماه گذشته: ${fmtToman(prev)}`
      : `${delta >= 0 ? "▲" : "▼"} ${fmtNum(Math.abs(delta))}٪ نسبت به ماه گذشته`;

  const trend = (d?.daily_sales ?? []).map((r) => ({
    day: r.day, date: r.date, amount_toman: r.amount_toman,
  }));
  const hasSales = trend.some((r) => r.amount_toman !== 0);
  const topPlans = d?.top_plans ?? [];
  const bestRevenue = topPlans[0]?.amount_toman || 1;
  const active = (d?.service_states?.provisioned ?? 0) + (d?.service_states?.renewing ?? 0);

  return (
    <DataState isLoading={query.isLoading} isError={query.isError} rows={6} onRetry={() => query.refetch()}>
      {d && (
        <Stack spacing={2}>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard label="فروش امروز" value={fmtToman(d.sales_today.net_sales_toman)}
                        icon={<ShoppingCartIcon />} color="#10b981" />
            </Grid>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard label="فروش این ماه" value={fmtToman(month)} sub={monthSub}
                        icon={<ShoppingCartIcon />} color="#7c5cff" />
            </Grid>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard label="سرویس‌های فعال" value={fmtNum(active)}
                        sub={d.near_expiry ? `${fmtNum(d.near_expiry)} سرویس نزدیک انقضا` : undefined}
                        icon={<AutorenewIcon />} color="#0071e3" />
            </Grid>
            <Grid item xs={12} sm={6} md={3}>
              <StatCard label="مشتریان" value={fmtNum(d.customers.total)}
                        sub={`${fmtNum(d.new_customers_range)} مشتری جدید این ماه`}
                        icon={<GroupIcon />} color="#f43f5e" />
            </Grid>
          </Grid>

          <SectionCard title="روند فروش روزانه">
            {hasSales ? (
              <EChart option={dailyTrendOption(theme, trend)} height={260} />
            ) : (
              <Typography color="text.secondary" sx={{ py: 4, textAlign: "center", fontSize: 14 }}>
                در این ماه هنوز فروشی ثبت نشده است.
              </Typography>
            )}
          </SectionCard>

          <Grid container spacing={2}>
            <Grid item xs={12} md={7}>
              <SectionCard title="پرفروش‌ترین پلن‌ها (این ماه)">
                {topPlans.length ? (
                  <Stack spacing={1.6}>
                    {topPlans.map((p) => (
                      <Box key={`${p.plan_id}-${p.title}`}>
                        <Stack direction="row" justifyContent="space-between" alignItems="baseline" spacing={1}>
                          <Typography variant="body2" sx={{ fontWeight: 600 }}>{p.title}</Typography>
                          <Typography variant="caption" color="text.secondary">
                            {fmtNum(p.orders)} فروش · {fmtToman(p.amount_toman)}
                          </Typography>
                        </Stack>
                        <LinearProgress
                          variant="determinate"
                          value={Math.max(3, Math.round((p.amount_toman / bestRevenue) * 100))}
                          sx={{ height: 8, borderRadius: 4, mt: 0.6 }}
                        />
                      </Box>
                    ))}
                  </Stack>
                ) : (
                  <Typography color="text.secondary" sx={{ py: 3, textAlign: "center", fontSize: 14 }}>
                    هنوز فروشی برای این ماه ثبت نشده است.
                  </Typography>
                )}
              </SectionCard>
            </Grid>

            <Grid item xs={12} md={5}>
              <Stack spacing={2}>
                <SectionCard title="وضعیت سرویس‌ها">
                  <Stack direction="row" gap={1} flexWrap="wrap">
                    {Object.entries(d.service_states)
                      .filter(([, n]) => Number(n) > 0)
                      .map(([state, n]) => (
                        <Chip key={state} size="small" variant="outlined"
                              color={state === "provisioned" ? "success" : state === "failed" ? "error" : "default"}
                              label={`${SERVICE_LABELS[state] || state}: ${fmtNum(Number(n))}`} />
                      ))}
                  </Stack>
                </SectionCard>

                <SectionCard title="خرید جدید در برابر تمدید (این ماه)">
                  <Stack spacing={1}>
                    <Row label="خرید جدید" value={d.sales_range.purchase.amount_toman} />
                    <Row label="تمدید" value={d.sales_range.renewal.amount_toman} />
                    {d.sales_range.reversals_toman > 0 && (
                      <Row label="برگشت وجه" value={-d.sales_range.reversals_toman} />
                    )}
                  </Stack>
                </SectionCard>

                <SectionCard title="تبدیل تستِ رایگان به خرید">
                  <Stack direction="row" alignItems="baseline" spacing={1}>
                    <Typography variant="h5" sx={{ fontWeight: 800 }}>
                      {d.trial_conversion.rate === null
                        ? "—"
                        : `${fmtNum(Math.round(d.trial_conversion.rate * 1000) / 10)}٪`}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {fmtNum(d.trial_conversion.converted_customers)} تبدیل از{" "}
                      {fmtNum(d.trial_conversion.trial_customers)} مشتری آزمایشی
                    </Typography>
                  </Stack>
                </SectionCard>

                <SectionCard title="کیف پول و شارژ">
                  <Stack spacing={1}>
                    <Row label="موجودی کیف پول مشتریان" value={d.customers.wallet_liability_toman} />
                    {d.pending_topups.count > 0 && (
                      <Stack direction="row" justifyContent="space-between">
                        <Typography variant="body2" color="text.secondary">شارژ در انتظار تأیید</Typography>
                        <Chip size="small" color="warning" label={fmtNum(d.pending_topups.count)} />
                      </Stack>
                    )}
                  </Stack>
                </SectionCard>
              </Stack>
            </Grid>
          </Grid>
        </Stack>
      )}
    </DataState>
  );
}

function Row({ label, value }: { label: string; value: number }) {
  return (
    <Stack direction="row" justifyContent="space-between" spacing={1}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{fmtToman(value)}</Typography>
    </Stack>
  );
}
