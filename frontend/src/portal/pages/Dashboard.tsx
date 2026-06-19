import { useState } from "react";
import { Alert, Box, Button, Chip, Grid, Skeleton, Stack, Typography } from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import TrendingUpIcon from "@mui/icons-material/esm/TrendingUp";
import DataUsageIcon from "@mui/icons-material/esm/DataUsage";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import GroupIcon from "@mui/icons-material/esm/Group";
import { useQuery } from "@tanstack/react-query";
import { portalSummary } from "../portalClient";
import StatCard, { currentPeriod } from "../../components/StatCard";
import PeriodPicker from "../../components/PeriodPicker";
import EChart from "../../components/EChart";
import { CountUp, Reveal } from "../../components/motion";
import { fmtGb, fmtNum, fmtToman } from "../../format";
import { SectionCard, EmptyState } from "../ui";

const FONT = "Vazirmatn, sans-serif";

export default function PortalDashboard() {
  const [period, setPeriod] = useState(currentPeriod());
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["portal-summary", period],
    queryFn: () => portalSummary(period),
  });

  const trend = data?.trend || [];
  const perReseller = data?.per_reseller || [];
  const accent = theme.palette.primary.main;
  const trendEmpty = !trend.length || trend.every((d) => !d.amount_toman);

  const fmtAxisToman = (v: number) =>
    v >= 1_000_000 ? `${fmtNum(Math.round(v / 1_000_000))}م`
      : v >= 1_000 ? `${fmtNum(Math.round(v / 1_000))}هزار` : fmtNum(v);

  const tooltip = {
    backgroundColor: isDark ? "rgba(14,16,32,0.88)" : "rgba(255,255,255,0.88)",
    borderColor: isDark ? "rgba(255,255,255,0.14)" : "rgba(200,210,255,0.55)",
    textStyle: { color: isDark ? "#e2e8f0" : "#334155", fontFamily: FONT },
  };
  const trendOption = {
    textStyle: { fontFamily: FONT },
    grid: { left: 6, right: 14, top: 18, bottom: 4, containLabel: true },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params;
        const row = trend[p.dataIndex];
        return `${row?.date ?? ""}<br/><b>${fmtToman(p.value || 0)}</b>`;
      },
      ...tooltip,
    },
    xAxis: {
      type: "category",
      data: trend.map((d) => fmtNum(d.day)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11,
        interval: trend.length > 16 ? 1 : 0,
      },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11, formatter: fmtAxisToman },
      splitLine: { lineStyle: { color: theme.palette.divider } },
    },
    series: [{
      type: "bar",
      data: trend.map((d) => d.amount_toman),
      barMaxWidth: 18,
      itemStyle: {
        borderRadius: [6, 6, 0, 0],
        color: {
          type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: accent },
            { offset: 1, color: alpha(accent, isDark ? 0.35 : 0.45) },
          ],
        },
      },
    }],
  };

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "stretch", sm: "center" }}
        spacing={1.5}
        sx={{ mb: 2.5 }}
      >
        <Box>
          <Typography variant="h5">نمای کلی شما</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
            برآوردِ فروشِ ماهِ جاری (شما + زیرمجموعه‌ها) و بدهیِ معوق
          </Typography>
        </Box>
        <PeriodPicker value={period} onChange={setPeriod} />
      </Stack>

      {isError ? (
        <Alert
          severity="error"
          action={<Button color="inherit" size="small" onClick={() => refetch()}>تلاش دوباره</Button>}
        >
          خطا در بارگذاری اطلاعات. اتصال را بررسی کنید.
        </Alert>
      ) : isLoading || !data ? (
        <Grid container spacing={2}>
          {[0, 1, 2, 3].map((i) => (
            <Grid item xs={12} sm={6} lg={3} key={i}>
              <Skeleton variant="rounded" height={154} animation="wave" sx={{ borderRadius: "18px" }} />
            </Grid>
          ))}
        </Grid>
      ) : (
        <>
          <Grid container spacing={2}>
            {[
              {
                label: "برآورد فروش ماه",
                value: <CountUp to={data.estimate.amount_toman} format={fmtToman} />,
                sub: <Typography variant="caption" color="text.secondary">دورهٔ {data.period}</Typography>,
                color: "#10b981",
                icon: <TrendingUpIcon />,
              },
              {
                label: "حجم فروخته‌شده",
                value: <CountUp to={data.estimate.gb} format={fmtGb} />,
                sub: <Typography variant="caption" color="text.secondary">{fmtNum(data.estimate.users)} سرویس</Typography>,
                color: "#0071e3",
                icon: <DataUsageIcon />,
              },
              {
                label: "بدهی معوق",
                value: <CountUp to={data.outstanding.amount_toman} format={fmtToman} />,
                sub: <Typography variant="caption" color="text.secondary">{fmtNum(data.outstanding.count)} فاکتور پرداخت‌نشده</Typography>,
                color: "#f43f5e",
                icon: <WarningAmberIcon />,
              },
              {
                label: "نمایندگی‌های شما",
                value: <CountUp to={data.reseller_count} format={fmtNum} />,
                sub: <Typography variant="caption" color="text.secondary">روی همهٔ پنل‌ها</Typography>,
                color: "#0ea5e9",
                icon: <GroupIcon />,
              },
            ].map((card) => (
              <Grid item xs={12} sm={6} lg={3} key={card.label}>
                <StatCard {...card} />
              </Grid>
            ))}
          </Grid>

          <Reveal delay={0.2}>
            <Grid container spacing={2} sx={{ mt: 0.5 }}>
              <Grid item xs={12} lg={7}>
                <SectionCard title="روند فروش روزانه" action={<Chip size="small" label={data.period} sx={{ fontWeight: 700 }} />}>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                    سهمِ هر روز از فروشِ این ماه (بر اساس تاریخِ ساختِ سرویس‌ها)
                  </Typography>
                  {trendEmpty ? (
                    <EmptyState>فروشی در این دوره ثبت نشده است.</EmptyState>
                  ) : (
                    <EChart option={trendOption} height={290} ariaLabel="روند فروش روزانه" />
                  )}
                </SectionCard>
              </Grid>

              <Grid item xs={12} lg={5}>
                <SectionCard title="تفکیک بر اساس نمایندگی">
                  {perReseller.length ? (
                    <Stack spacing={1.4}>
                      {perReseller.map((r) => (
                        <Stack
                          key={r.id}
                          direction="row"
                          alignItems="center"
                          justifyContent="space-between"
                          spacing={1.5}
                          sx={{ p: 1.3, borderRadius: 2, bgcolor: alpha(theme.palette.text.secondary, isDark ? 0.06 : 0.05) }}
                        >
                          <Box sx={{ minWidth: 0 }}>
                            <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>{r.name}</Typography>
                            <Typography variant="caption" color="text.secondary">
                              {r.panel_key} · {fmtGb(r.gb)} · {fmtNum(r.users)} سرویس
                            </Typography>
                          </Box>
                          <Typography variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>
                            {fmtToman(r.amount_toman)}
                          </Typography>
                        </Stack>
                      ))}
                    </Stack>
                  ) : (
                    <EmptyState>نمایندگی‌ای یافت نشد.</EmptyState>
                  )}
                </SectionCard>
              </Grid>
            </Grid>
          </Reveal>
        </>
      )}
    </Box>
  );
}
