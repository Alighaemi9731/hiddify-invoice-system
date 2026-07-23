import { ReactNode, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Grid,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import ArrowDownwardIcon from "@mui/icons-material/esm/ArrowDownward";
import ArrowUpwardIcon from "@mui/icons-material/esm/ArrowUpward";
import CheckCircleOutlineIcon from "@mui/icons-material/esm/CheckCircleOutline";
import DnsIcon from "@mui/icons-material/esm/Dns";
import GroupIcon from "@mui/icons-material/esm/Group";
import TrendingUpIcon from "@mui/icons-material/esm/TrendingUp";
import WarningAmberIcon from "@mui/icons-material/esm/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { getDashboard, getSalesByDay } from "../api/client";
import StatCard, { currentPeriod } from "../components/StatCard";
import PeriodPicker from "../components/PeriodPicker";
import EChart from "../components/EChart";
import { chartTooltip } from "../components/chartTooltip";
import { SectionCard, EmptyState } from "../portal/ui";
import { CHART_FONT, EASE_ENTRANCE, invoiceStatusColor } from "../themeTokens";
import { CountUp, Reveal } from "../components/motion";
import { fmtGb, fmtNum, fmtToman, INVOICE_STATUS_FA } from "../format";

const EASE = EASE_ENTRANCE;
const FONT = CHART_FONT;
const RANK_COLORS = ["#0071e3", "#30d158", "#ff9500", "#32ade6", "#bf5af2"];
const STATUS_ORDER = ["paid", "sent", "overdue", "enforced"];

function formatPercent(value: number) {
  return value.toLocaleString("fa-IR", { maximumFractionDigits: 1 });
}

function MetricDetail({
  children,
  color,
  icon,
}: {
  children: ReactNode;
  color?: string;
  icon?: ReactNode;
}) {
  return (
    <Stack direction="row" spacing={0.5} alignItems="center" sx={{ color: color || "text.secondary" }}>
      {icon}
      <Typography component="span" variant="caption" sx={{ color: "inherit", fontWeight: 650 }}>
        {children}
      </Typography>
    </Stack>
  );
}

export default function Dashboard() {
  const [period, setPeriod] = useState(currentPeriod());
  const theme = useTheme();
  const isDark = theme.palette.mode === "dark";
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["dashboard", period],
    queryFn: () => getDashboard(period),
  });
  const { data: salesByDay = [], isLoading: salesByDayLoading } = useQuery({
    queryKey: ["sales-by-day", period],
    queryFn: () => getSalesByDay(period),
  });

  const panelData = data?.sales_by_panel || [];
  const topResellers = data?.top_resellers || [];
  const statusData = [...(data?.status_counts || [])]
    .sort((a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status))
    .map((item) => ({
      ...item,
      label: INVOICE_STATUS_FA[item.status] || item.status,
      color: invoiceStatusColor(theme.palette, item.status),
    }));

  const periodInvoices = data?.period_invoices || 0;
  const collectionRate = data?.period_billed_toman
    ? (data.period_paid_toman / data.period_billed_toman) * 100
    : 0;
  const previousSales = data?.previous_period_billed_toman || 0;
  const salesChange = previousSales && data
    ? ((data.period_billed_toman - previousSales) / previousSales) * 100
    : null;
  const maxPanelSales = Math.max(...panelData.map((item) => item.amount_toman), 1);
  const maxResellerSales = Math.max(...topResellers.map((item) => item.amount_toman), 1);

  const tooltip = chartTooltip(theme);
  // Compact Toman for the y-axis (full amount stays in the tooltip).
  const fmtAxisToman = (v: number) =>
    v >= 1_000_000 ? `${fmtNum(Math.round(v / 1_000_000))}م`
      : v >= 1_000 ? `${fmtNum(Math.round(v / 1_000))}هزار`
        : fmtNum(v);
  const accent = theme.palette.primary.main;
  const salesDayEmpty = !salesByDay.length || salesByDay.every((d: any) => !d.amount_toman);
  // Memoized: EChart re-applies setOption whenever the option object identity changes,
  // so rebuilding these on every render forced a full canvas redraw per re-render.
  const salesByDayOption = useMemo(() => ({
    textStyle: { fontFamily: FONT },
    grid: { left: 6, right: 14, top: 18, bottom: 4, containLabel: true },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params;
        const row = salesByDay[p.dataIndex];
        return `${row?.date ?? ""}<br/><b>${fmtToman(p.value || 0)}</b>`;
      },
      ...tooltip,
    },
    xAxis: {
      type: "category",
      data: salesByDay.map((d: any) => fmtNum(d.day)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11,
        interval: salesByDay.length > 16 ? 1 : 0,
      },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11,
        formatter: fmtAxisToman,
      },
      splitLine: { lineStyle: { color: theme.palette.divider } },
    },
    series: [{
      type: "bar",
      data: salesByDay.map((d: any) => d.amount_toman),
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
      emphasis: {
        itemStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: accent },
              { offset: 1, color: alpha(accent, 0.7) },
            ],
          },
        },
      },
    }],
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [salesByDay, isDark, accent, theme]);
  const statusOption = useMemo(() => ({
    textStyle: { fontFamily: FONT },
    tooltip: {
      trigger: "item",
      formatter: (params: any) => `${params.name}: ${fmtNum(params.value)}`,
      ...tooltip,
    },
    series: [{
      type: "pie",
      radius: ["66%", "84%"],
      center: ["50%", "50%"],
      silent: false,
      label: { show: false },
      itemStyle: {
        borderRadius: 7,
        borderColor: theme.palette.background.paper,
        borderWidth: 3,
      },
      data: statusData.map((item) => ({
        value: item.count,
        name: item.label,
        itemStyle: { color: item.color },
      })),
    }],
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [statusData.map((i) => `${i.status}:${i.count}`).join(","), isDark, theme]);

  const salesTrend = salesChange === null ? (
    <MetricDetail>{data?.period_billed_toman ? "اولین فروش ثبت‌شده" : "بدون فروش در دورهٔ قبل"}</MetricDetail>
  ) : salesChange >= 0 ? (
    <MetricDetail color={theme.palette.success.main} icon={<ArrowUpwardIcon sx={{ fontSize: 15 }} />}>
      {formatPercent(salesChange)}٪ نسبت به دورهٔ قبل
    </MetricDetail>
  ) : (
    <MetricDetail color={theme.palette.error.main} icon={<ArrowDownwardIcon sx={{ fontSize: 15 }} />}>
      {formatPercent(Math.abs(salesChange))}٪ نسبت به دورهٔ قبل
    </MetricDetail>
  );

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
          <Typography variant="h5">نمای کلی عملکرد</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
            آمار فروش، وصول و وضعیت نمایندگان در دورهٔ انتخاب‌شده
          </Typography>
        </Box>
        <PeriodPicker value={period} onChange={setPeriod} />
      </Stack>

      {isError ? (
        <Alert
          severity="error"
          action={<Button color="inherit" size="small" onClick={() => refetch()}>تلاش دوباره</Button>}
        >
          خطا در بارگذاری داشبورد. اتصال را بررسی کنید.
        </Alert>
      ) : isLoading || !data ? (
        <>
          <Grid container spacing={2}>
            {[0, 1, 2, 3].map((item) => (
              <Grid item xs={12} sm={6} lg={3} key={item}>
                <Skeleton variant="rounded" height={154} animation="wave" sx={{ borderRadius: "18px" }} />
              </Grid>
            ))}
          </Grid>
          <Grid container spacing={2} sx={{ mt: 0.5 }}>
            <Grid item xs={12} lg={8}>
              <Skeleton variant="rounded" height={390} animation="wave" sx={{ borderRadius: "18px" }} />
            </Grid>
            <Grid item xs={12} lg={4}>
              <Skeleton variant="rounded" height={390} animation="wave" sx={{ borderRadius: "18px" }} />
            </Grid>
            <Grid item xs={12}>
              <Skeleton variant="rounded" height={360} animation="wave" sx={{ borderRadius: "18px" }} />
            </Grid>
          </Grid>
        </>
      ) : (
        <>
          <Grid container spacing={2}>
            {[
              {
                label: "پنل‌ها",
                value: <CountUp to={data.panels} format={fmtNum} />,
                sub: (
                  <MetricDetail
                    color={data.healthy_panels === data.active_panels
                      ? theme.palette.success.main
                      : theme.palette.warning.main}
                    icon={<CheckCircleOutlineIcon sx={{ fontSize: 15 }} />}
                  >
                    {fmtNum(data.active_panels)} فعال · {fmtNum(data.healthy_panels)} سالم
                  </MetricDetail>
                ),
                color: "#0071e3",
                icon: <DnsIcon />,
              },
              {
                label: "نمایندگان اصلی",
                value: <CountUp to={data.resellers} format={fmtNum} />,
                sub: <MetricDetail>{fmtNum(data.registered_resellers)} متصل به ربات</MetricDetail>,
                color: "#0ea5e9",
                icon: <GroupIcon />,
              },
              {
                label: "فروش این دوره",
                value: <CountUp to={data.period_billed_toman} format={fmtToman} />,
                sub: salesTrend,
                color: "#10b981",
                icon: <TrendingUpIcon />,
              },
              {
                label: "بدهی معوق",
                value: <CountUp to={data.outstanding_toman} format={fmtToman} />,
                sub: <MetricDetail>{fmtNum(data.outstanding_resellers)} نمایندهٔ بدهکار</MetricDetail>,
                color: "#f43f5e",
                icon: <WarningAmberIcon />,
              },
            ].map((card, index) => (
              <Grid item xs={12} sm={6} lg={3} key={card.label}>
                <motion.div
                  initial={{ opacity: 0, y: 18 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.45, ease: EASE, delay: index * 0.07 }}
                  style={{ height: "100%" }}
                >
                  <StatCard {...card} />
                </motion.div>
              </Grid>
            ))}
          </Grid>

          <Reveal delay={0.3}>
            <Grid container spacing={2} sx={{ mt: 0.5 }}>
              <Grid item xs={12} lg={8}>
                <SectionCard
                  title="فروش بر اساس پنل"
                  action={<Chip size="small" label={data.period} sx={{ fontWeight: 700 }} />}
                >
                  {panelData.length ? (
                    <Stack spacing={2.2}>
                      {panelData.map((panel, index) => {
                        const color = RANK_COLORS[index % RANK_COLORS.length];
                        const width = Math.max((panel.amount_toman / maxPanelSales) * 100, 3);
                        return (
                          <Box key={panel.panel_id}>
                            <Stack direction="row" justifyContent="space-between" alignItems="flex-end" spacing={2}>
                              <Box sx={{ minWidth: 0 }}>
                                <Typography variant="body2" sx={{ fontWeight: 800 }}>
                                  {panel.panel_key}
                                </Typography>
                                <Typography variant="caption" color="text.secondary">
                                  {fmtNum(panel.invoices)} فاکتور · {fmtGb(panel.usage_gb)}
                                </Typography>
                              </Box>
                              <Typography variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>
                                {fmtToman(panel.amount_toman)}
                              </Typography>
                            </Stack>
                            <Box
                              sx={{
                                mt: 1,
                                height: 9,
                                borderRadius: 980,
                                bgcolor: alpha(theme.palette.text.secondary, 0.11),
                                display: "flex",
                                justifyContent: "flex-end",
                                overflow: "hidden",
                              }}
                            >
                              <motion.div
                                initial={{ width: 0 }}
                                animate={{ width: `${width}%` }}
                                transition={{ duration: 0.65, delay: 0.15 + index * 0.06, ease: EASE }}
                                style={{
                                  height: "100%",
                                  borderRadius: 980,
                                  background: `linear-gradient(90deg, ${alpha(color, 0.72)}, ${color})`,
                                }}
                              />
                            </Box>
                          </Box>
                        );
                      })}
                    </Stack>
                  ) : (
                    <EmptyState>برای این دوره هنوز فروش ثبت نشده است.</EmptyState>
                  )}
                </SectionCard>
              </Grid>

              <Grid item xs={12} lg={4}>
                <SectionCard
                  title="توزیع وضعیت فاکتورها"
                  action={
                    <Chip
                      size="small"
                      color="success"
                      variant="outlined"
                      label={`${formatPercent(collectionRate)}٪ وصول`}
                      sx={{ fontWeight: 750 }}
                    />
                  }
                >
                  {statusData.length ? (
                    <Stack direction={{ xs: "column", sm: "row", lg: "column" }} alignItems="center" spacing={2}>
                      <Box sx={{ width: 190, height: 190, position: "relative", flexShrink: 0 }}>
                        <EChart
                          option={statusOption}
                          height={190}
                          ariaLabel={`توزیع وضعیت ${fmtNum(periodInvoices)} فاکتور دوره`}
                        />
                        <Box
                          sx={{
                            position: "absolute",
                            inset: 0,
                            display: "grid",
                            placeContent: "center",
                            textAlign: "center",
                            pointerEvents: "none",
                          }}
                        >
                          <Typography sx={{ fontSize: 27, fontWeight: 850, lineHeight: 1 }}>
                            {fmtNum(periodInvoices)}
                          </Typography>
                          <Typography variant="caption" color="text.secondary" sx={{ mt: 0.6 }}>
                            فاکتور
                          </Typography>
                        </Box>
                      </Box>
                      <Box
                        sx={{
                          width: "100%",
                          display: "grid",
                          gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "1fr", lg: "repeat(2, minmax(0, 1fr))" },
                          gap: 1.1,
                        }}
                      >
                        {statusData.map((item) => (
                          <Stack
                            key={item.status}
                            direction="row"
                            alignItems="center"
                            justifyContent="space-between"
                            spacing={1}
                            sx={{
                              p: 1,
                              borderRadius: 2,
                              bgcolor: alpha(item.color, isDark ? 0.09 : 0.07),
                            }}
                          >
                            <Stack direction="row" alignItems="center" spacing={0.8} sx={{ minWidth: 0 }}>
                              <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: item.color, flexShrink: 0 }} />
                              <Typography variant="caption" noWrap>{item.label}</Typography>
                            </Stack>
                            <Typography variant="body2" sx={{ fontWeight: 850 }}>{fmtNum(item.count)}</Typography>
                          </Stack>
                        ))}
                      </Box>
                    </Stack>
                  ) : (
                    <EmptyState>هنوز فاکتور ارسال‌شده‌ای در این دوره نیست.</EmptyState>
                  )}
                </SectionCard>
              </Grid>

              <Grid item xs={12}>
                <SectionCard
                  title="۱۰ نمایندهٔ برتر دوره"
                  action={
                    <Typography variant="caption" color="text.secondary">
                      مرتب‌شده بر اساس مبلغ فروش
                    </Typography>
                  }
                >
                  {topResellers.length ? (
                    <Box
                      sx={{
                        display: "grid",
                        gridTemplateColumns: { xs: "1fr", lg: "repeat(2, minmax(0, 1fr))" },
                        columnGap: 4,
                        rowGap: 1.3,
                      }}
                    >
                      {topResellers.map((reseller, index) => {
                        const color = RANK_COLORS[index % RANK_COLORS.length];
                        const width = Math.max((reseller.amount_toman / maxResellerSales) * 100, 4);
                        return (
                          <Box
                            key={reseller.invoice_id}
                            sx={{
                              display: "grid",
                              gridTemplateColumns: "34px minmax(105px, auto) minmax(90px, 1fr) auto",
                              alignItems: "center",
                              gap: { xs: 1, sm: 1.5 },
                              minWidth: 0,
                              py: 0.65,
                            }}
                          >
                            <Box
                              sx={{
                                width: 31,
                                height: 31,
                                borderRadius: 2,
                                display: "grid",
                                placeItems: "center",
                                bgcolor: alpha(color, 0.12),
                                color,
                                fontSize: 12,
                                fontWeight: 850,
                              }}
                            >
                              {fmtNum(index + 1)}
                            </Box>
                            <Box sx={{ minWidth: 0 }}>
                              <Typography variant="body2" noWrap sx={{ fontWeight: 750 }}>
                                {reseller.reseller_name}
                              </Typography>
                              <Typography variant="caption" color="text.secondary">
                                {reseller.panel_key} · {fmtGb(reseller.usage_gb)}
                              </Typography>
                            </Box>
                            <Box
                              sx={{
                                height: 8,
                                borderRadius: 980,
                                bgcolor: alpha(theme.palette.text.secondary, 0.11),
                                display: "flex",
                                justifyContent: "flex-end",
                                overflow: "hidden",
                              }}
                            >
                              <Box
                                sx={{
                                  width: `${width}%`,
                                  height: "100%",
                                  borderRadius: 980,
                                  bgcolor: color,
                                }}
                              />
                            </Box>
                            <Typography variant="caption" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>
                              {fmtToman(reseller.amount_toman)}
                            </Typography>
                          </Box>
                        );
                      })}
                    </Box>
                  ) : (
                    <EmptyState>برای رتبه‌بندی این دوره داده‌ای وجود ندارد.</EmptyState>
                  )}
                </SectionCard>
              </Grid>

              <Grid item xs={12}>
                <SectionCard
                  title="روند فروش روزانه"
                  action={<Chip size="small" label={data.period} sx={{ fontWeight: 700 }} />}
                >
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                    سهم هر روز از فروش این ماه (بر اساس تاریخ ساخت سرویس‌ها)
                  </Typography>
                  {salesByDayLoading ? (
                    <Skeleton variant="rounded" height={300} />
                  ) : salesDayEmpty ? (
                    <EmptyState>فروشی در این دوره ثبت نشده است.</EmptyState>
                  ) : (
                    <EChart option={salesByDayOption} height={300} ariaLabel="روند فروش روزانه" />
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
