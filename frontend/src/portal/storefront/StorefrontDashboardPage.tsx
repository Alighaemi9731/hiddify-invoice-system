import { Box, Chip, Grid, Stack, Typography } from "@mui/material";
import ShoppingCartIcon from "@mui/icons-material/esm/ShoppingCart";
import AccountBalanceWalletIcon from "@mui/icons-material/esm/AccountBalanceWallet";
import GroupIcon from "@mui/icons-material/esm/Group";
import AutorenewIcon from "@mui/icons-material/esm/Autorenew";
import { useQuery } from "@tanstack/react-query";
import { alpha, useTheme } from "@mui/material/styles";
import { useOutletContext } from "react-router-dom";
import { DataState } from "../../components/DataState";
import StatCard from "../../components/StatCard";
import EChart from "../../components/EChart";
import { fmtNum, fmtToman } from "../../format";
import { SectionCard } from "../ui";
import { currentTehranMonthRange, getStorefrontDashboard, storefrontQueryKeys } from "./api";
import type { StorefrontOutletContext } from "./StorefrontShell";

const SERVICE_LABELS: Record<string, string> = {
  pending: "در انتظار",
  renewing: "در حال تمدید",
  provisioned: "فعال",
  disabled: "غیرفعال",
  failed: "ناموفق",
  deleted: "حذف‌شده",
};

export default function StorefrontDashboardPage() {
  const { shop } = useOutletContext<StorefrontOutletContext>();
  const theme = useTheme();
  const { from, to } = currentTehranMonthRange();
  const query = useQuery({
    queryKey: storefrontQueryKeys.dashboard(shop.id, from, to),
    queryFn: () => getStorefrontDashboard(shop.id, from, to),
  });
  const breakdown = query.data ? [
    { label: "خرید جدید", value: query.data.sales_range.purchase.amount_toman, color: "#10b981" },
    { label: "تمدید", value: query.data.sales_range.renewal.amount_toman, color: "#0071e3" },
    { label: "قدیمی / نامشخص", value: query.data.sales_range.unknown.amount_toman, color: "#f59e0b" },
  ] : [];
  const breakdownChart = {
    textStyle: { fontFamily: "Vazirmatn, sans-serif" },
    grid: { left: 8, right: 12, top: 12, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params: Array<{ dataIndex: number }>) => {
        const row = breakdown[params[0]?.dataIndex];
        return row ? `${row.label}<br/><b>${fmtToman(row.value)}</b>` : "";
      },
    },
    xAxis: {
      type: "value",
      axisLabel: { color: theme.palette.text.secondary, formatter: (value: number) => fmtNum(value) },
      splitLine: { lineStyle: { color: theme.palette.divider } },
    },
    yAxis: {
      type: "category",
      data: breakdown.map((row) => row.label),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: { color: theme.palette.text.secondary, fontFamily: "Vazirmatn, sans-serif" },
    },
    series: [{
      type: "bar",
      data: breakdown.map((row) => ({ value: row.value, itemStyle: { color: row.color, borderRadius: [0, 6, 6, 0] } })),
      barMaxWidth: 30,
    }],
  };

  return (
    <DataState
      isLoading={query.isLoading}
      isError={query.isError}
      rows={7}
      onRetry={() => query.refetch()}
    >
      {query.data && (
        <Box>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={6} lg={3}>
              <StatCard label="فروش امروز" value={fmtToman(query.data.sales_today.net_sales_toman)} color="#10b981" icon={<ShoppingCartIcon />} />
            </Grid>
            <Grid item xs={12} sm={6} lg={3}>
              <StatCard label="فروش خالص ماه" value={fmtToman(query.data.sales_month.net_sales_toman)} sub={`فروش دوره: ${fmtToman(query.data.sales_range.net_sales_toman)}`} color="#0071e3" icon={<AutorenewIcon />} />
            </Grid>
            <Grid item xs={12} sm={6} lg={3}>
              <StatCard label="مشتریان" value={fmtNum(query.data.customers.total)} sub={`${fmtNum(query.data.customers.active_30d)} فعال در ۳۰ روز`} color="#a855f7" icon={<GroupIcon />} />
            </Grid>
            <Grid item xs={12} sm={6} lg={3}>
              <StatCard label="تعهد کیف پول" value={fmtToman(query.data.customers.wallet_liability_toman)} sub={`${fmtNum(query.data.pending_topups.count)} شارژ در انتظار`} color="#f59e0b" icon={<AccountBalanceWalletIcon />} />
            </Grid>
          </Grid>

          <Grid container spacing={2} sx={{ mt: 0.5 }}>
            <Grid item xs={12}>
              <SectionCard title="نمودار تفکیک فروش دوره">
                <EChart option={breakdownChart} height={260} ariaLabel="نمودار مبلغ خرید، تمدید و فروش قدیمی دوره" />
              </SectionCard>
            </Grid>
            <Grid item xs={12} lg={6}>
              <SectionCard title="تفکیک فروش دوره">
                <Stack spacing={1.5}>
                  <MetricRow label="خرید جدید" count={query.data.sales_range.purchase.count} amount={query.data.sales_range.purchase.amount_toman} />
                  <MetricRow label="تمدید" count={query.data.sales_range.renewal.count} amount={query.data.sales_range.renewal.amount_toman} />
                  <MetricRow label="قدیمی / نامشخص" count={query.data.sales_range.unknown.count} amount={query.data.sales_range.unknown.amount_toman} />
                  <MetricRow label="برگشت وجه" count={null} amount={query.data.sales_range.reversals_toman} />
                </Stack>
              </SectionCard>
            </Grid>
            <Grid item xs={12} lg={6}>
              <SectionCard title="وضعیت سرویس‌ها">
                <Stack direction="row" gap={1} flexWrap="wrap">
                  {Object.entries(query.data.service_states).map(([state, count]) => (
                    <Chip
                      key={state}
                      color={state === "failed" ? "error" : state === "provisioned" ? "success" : "default"}
                      variant="outlined"
                      label={`${SERVICE_LABELS[state] || state}: ${fmtNum(count)}`}
                    />
                  ))}
                  <Chip color="warning" variant="outlined" label={`نزدیک انقضا: ${fmtNum(query.data.near_expiry)}`} />
                </Stack>
              </SectionCard>
            </Grid>
            <Grid item xs={12} md={6}>
              <SectionCard title="شارژ و اعتبار">
                <Stack spacing={1.5}>
                  <MetricRow label="مبلغ شارژهای در انتظار" count={query.data.pending_topups.count} amount={query.data.pending_topups.amount_toman} />
                  <MetricRow label="استفاده از کد اعتبار" count={query.data.credits.redemptions} amount={query.data.credits.bonus_toman} />
                </Stack>
              </SectionCard>
            </Grid>
            <Grid item xs={12} md={6}>
              <SectionCard title="تبدیل دورهٔ آزمایشی">
                <Stack spacing={1}>
                  <Typography variant="h4" sx={{ fontWeight: 850 }}>
                    {query.data.trial_conversion.rate == null
                      ? "—"
                      : `${fmtNum(query.data.trial_conversion.rate * 100)}٪`}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {fmtNum(query.data.trial_conversion.converted_customers)} تبدیل از {fmtNum(query.data.trial_conversion.trial_customers)} مشتری آزمایشی
                  </Typography>
                </Stack>
              </SectionCard>
            </Grid>
          </Grid>
        </Box>
      )}
    </DataState>
  );
}

function MetricRow({ label, count, amount }: { label: string; count: number | null; amount: number }) {
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={2}>
      <Box>
        <Typography variant="body2" sx={{ fontWeight: 700 }}>{label}</Typography>
        {count != null && <Typography variant="caption" color="text.secondary">{fmtNum(count)} مورد</Typography>}
      </Box>
      <Typography variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>{fmtToman(amount)}</Typography>
    </Stack>
  );
}
