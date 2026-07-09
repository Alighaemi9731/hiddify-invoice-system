import type { Theme } from "@mui/material/styles";
import { alpha } from "@mui/material/styles";
import { fmtNum, fmtToman } from "../format";

const FONT = "Vazirmatn, sans-serif";
const faDigits = (s: string) => s.replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d]);

export interface MonthlyRow { label: string; amount_toman: number; gb: number; new_services: number }

// ECharts option for the «فروش ماهانه» bar chart on the portal Dashboard (own + sub-resellers).
// Mirrors dailyTrend.ts so the two charts look like one system (gradient bars, shadow tooltip,
// dark/light aware). Month labels are YYYY-MM with Persian digits.
export function monthlyTrendOption(theme: Theme, rows: MonthlyRow[]) {
  const isDark = theme.palette.mode === "dark";
  const accent = theme.palette.primary.main;
  const fmtAxisToman = (v: number) =>
    v >= 1_000_000 ? `${fmtNum(Math.round(v / 1_000_000))}م`
      : v >= 1_000 ? `${fmtNum(Math.round(v / 1_000))}هزار` : fmtNum(v);
  const tooltip = {
    backgroundColor: isDark ? "rgba(14,16,32,0.88)" : "rgba(255,255,255,0.88)",
    borderColor: isDark ? "rgba(255,255,255,0.14)" : "rgba(200,210,255,0.55)",
    textStyle: { color: isDark ? "#e2e8f0" : "#334155", fontFamily: FONT },
  };
  return {
    textStyle: { fontFamily: FONT },
    grid: { left: 4, right: 10, top: 18, bottom: 2, containLabel: true },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params;
        const row = rows[p.dataIndex];
        if (!row) return "";
        return `${faDigits(row.label)}<br/><b>${fmtToman(row.amount_toman)}</b>`
          + `<br/>${fmtNum(row.gb)} گیگ · ${fmtNum(row.new_services)} سرویس`;
      },
      ...tooltip,
    },
    xAxis: {
      type: "category",
      data: rows.map((d) => faDigits(d.label)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11 },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 11, formatter: fmtAxisToman,
      },
      splitLine: { lineStyle: { color: theme.palette.divider } },
    },
    series: [{
      type: "bar",
      data: rows.map((d) => d.amount_toman),
      barMaxWidth: 34,
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
}
