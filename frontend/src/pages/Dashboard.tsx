import { useState } from "react";
import {
  Grid, Box, Card, CardContent, Typography, Stack, CircularProgress,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import DnsIcon from "@mui/icons-material/Dns";
import GroupIcon from "@mui/icons-material/Group";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { getDashboard } from "../api/client";
import StatCard, { currentPeriod } from "../components/StatCard";
import PeriodPicker from "../components/PeriodPicker";
import EChart from "../components/EChart";
import { fmtToman, fmtUsdt, fmtNum, fmtCompact, INVOICE_STATUS_FA } from "../format";
import { CHART_COLORS } from "../theme";

const FONT = "Vazirmatn, sans-serif";
const grad = (c1: string, c2: string, horizontal = false) => ({
  type: "linear", x: 0, y: 0, x2: horizontal ? 1 : 0, y2: horizontal ? 0 : 1,
  colorStops: [{ offset: 0, color: c1 }, { offset: 1, color: c2 }],
});

function ChartCard({ title, children }: { title: string; children: any }) {
  return (
    <Card sx={{ height: "100%", boxShadow: "0 1px 3px rgba(16,24,40,.06)" }}>
      <CardContent>
        <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 1.5 }}>{title}</Typography>
        {children}
      </CardContent>
    </Card>
  );
}

export default function Dashboard() {
  const [period, setPeriod] = useState(currentPeriod());
  const muiTheme = useTheme();
  const isDark = muiTheme.palette.mode === "dark";
  const { data, isLoading } = useQuery({ queryKey: ["dashboard", period], queryFn: () => getDashboard(period) });

  // theme-aware chart colors
  const axisText = isDark ? "#94a3b8" : "#475569";
  const mutedText = isDark ? "#64748b" : "#94a3b8";
  const splitLine = isDark ? "rgba(148,163,184,.14)" : "#eef2f7";
  const labelText = isDark ? "#cbd5e1" : "#64748b";
  const paper = muiTheme.palette.background.paper;
  const tooltip = {
    backgroundColor: isDark ? "#1e293b" : "#fff",
    borderColor: isDark ? "#334155" : "#e2e8f0",
    textStyle: { color: isDark ? "#e2e8f0" : "#334155", fontFamily: FONT },
  };

  const panelData = data?.sales_by_panel || [];
  const statusData = (data?.status_counts || []).map((s: any) => ({
    value: s.count, name: INVOICE_STATUS_FA[s.status] || s.status,
  }));
  const top = data?.top_resellers || [];

  const salesByPanelOption = {
    textStyle: { fontFamily: FONT },
    grid: { left: 6, right: 18, top: 28, bottom: 6, containLabel: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v: any) => fmtToman(v), ...tooltip },
    xAxis: { type: "category", data: panelData.map((p: any) => p.panel_key), axisTick: { show: false }, axisLine: { lineStyle: { color: splitLine } }, axisLabel: { color: axisText, fontFamily: FONT } },
    yAxis: { type: "value", axisLabel: { formatter: fmtCompact, color: mutedText }, splitLine: { lineStyle: { color: splitLine } } },
    series: [{
      type: "bar", barMaxWidth: 56, data: panelData.map((p: any) => p.amount_toman),
      itemStyle: { borderRadius: [8, 8, 0, 0], color: grad("#3b5fb0", "#1f3b73") },
      label: { show: true, position: "top", formatter: (p: any) => fmtCompact(p.value), color: labelText, fontFamily: FONT },
    }],
  };

  const statusOption = {
    textStyle: { fontFamily: FONT },
    tooltip: { trigger: "item", ...tooltip },
    legend: { bottom: 0, icon: "circle", textStyle: { fontFamily: FONT, color: axisText } },
    series: [{
      type: "pie", radius: ["48%", "72%"], center: ["50%", "44%"], avoidLabelOverlap: true,
      itemStyle: { borderRadius: 8, borderColor: paper, borderWidth: 3 },
      label: { show: true, formatter: "{b}\n{c}", fontFamily: FONT, color: axisText },
      data: statusData.map((s: any, i: number) => ({ ...s, itemStyle: { color: CHART_COLORS[i % CHART_COLORS.length] } })),
    }],
  };

  const topOption = {
    textStyle: { fontFamily: FONT },
    grid: { left: 6, right: 52, top: 8, bottom: 6, containLabel: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v: any) => fmtToman(v), ...tooltip },
    xAxis: { type: "value", axisLabel: { formatter: fmtCompact, color: mutedText }, splitLine: { lineStyle: { color: splitLine } } },
    yAxis: { type: "category", data: top.map((t: any) => t.reseller_name).reverse(), axisTick: { show: false }, axisLine: { lineStyle: { color: splitLine } }, axisLabel: { color: axisText, fontFamily: FONT } },
    series: [{
      type: "bar", barMaxWidth: 22, data: top.map((t: any) => t.amount_toman).reverse(),
      itemStyle: { borderRadius: [0, 6, 6, 0], color: grad("#f7b733", "#f29f05", true) },
      label: { show: true, position: "right", formatter: (p: any) => fmtCompact(p.value), color: labelText, fontFamily: FONT },
    }],
  };

  return (
    <Box>
      <Stack direction="row" spacing={1.5} sx={{ mb: 3, flexWrap: "wrap", rowGap: 1.5 }} alignItems="center">
        <PeriodPicker value={period} onChange={setPeriod} />
      </Stack>

      {isLoading || !data ? (
        <Box sx={{ display: "grid", placeItems: "center", py: 8 }}><CircularProgress /></Box>
      ) : (
        <>
          <Grid container spacing={2}>
            <Grid item xs={6} md={3}><StatCard label="پنل‌ها" value={fmtNum(data.panels)} color="#3b82f6" icon={<DnsIcon />} /></Grid>
            <Grid item xs={6} md={3}><StatCard label="نمایندگان" value={fmtNum(data.resellers)} sub={`${fmtNum(data.registered_resellers)} متصل به ربات`} color="#0891b2" icon={<GroupIcon />} /></Grid>
            <Grid item xs={6} md={3}><StatCard label="فروش دوره" value={fmtToman(data.period_billed_toman)} sub={fmtUsdt(data.period_billed_usdt)} color="#f29f05" icon={<TrendingUpIcon />} /></Grid>
            <Grid item xs={6} md={3}><StatCard label="بدهی معوق" value={fmtToman(data.outstanding_toman)} sub={fmtUsdt(data.outstanding_usdt)} color="#ef4444" icon={<WarningAmberIcon />} /></Grid>
          </Grid>

          <Grid container spacing={2} sx={{ mt: 0.5 }}>
            <Grid item xs={12} md={7}>
              <ChartCard title="فروش بر اساس پنل">
                {panelData.length ? <EChart option={salesByPanelOption} height={300} /> :
                  <Box sx={{ height: 300, display: "grid", placeItems: "center", color: "text.secondary" }}>داده‌ای نیست</Box>}
              </ChartCard>
            </Grid>
            <Grid item xs={12} md={5}>
              <ChartCard title="وضعیت فاکتورها">
                {statusData.length ? <EChart option={statusOption} height={300} /> :
                  <Box sx={{ height: 300, display: "grid", placeItems: "center", color: "text.secondary" }}>هنوز فاکتور ارسال‌شده‌ای در این دوره نیست</Box>}
              </ChartCard>
            </Grid>
            <Grid item xs={12}>
              <ChartCard title="۱۰ نماینده برتر دوره">
                {top.length ? <EChart option={topOption} height={Math.max(240, top.length * 40)} /> :
                  <Box sx={{ height: 240, display: "grid", placeItems: "center", color: "text.secondary" }}>داده‌ای نیست</Box>}
              </ChartCard>
            </Grid>
          </Grid>
        </>
      )}
    </Box>
  );
}
