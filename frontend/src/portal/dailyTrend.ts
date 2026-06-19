import type { Theme } from "@mui/material/styles";
import { alpha } from "@mui/material/styles";
import { fmtNum, fmtToman } from "../format";

const FONT = "Vazirmatn, sans-serif";

export interface DailyRow { day: number; date: string; amount_toman: number }

// Shared ECharts option for the «روند فروش روزانه» bar chart — used by the portal Dashboard and each
// sub-reseller card so they look identical (gradient bars, shadow tooltip, axis, dark/light aware).
export function dailyTrendOption(theme: Theme, rows: DailyRow[], opts: { compact?: boolean } = {}) {
  const isDark = theme.palette.mode === "dark";
  const accent = theme.palette.primary.main;
  const compact = !!opts.compact;
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
    grid: { left: 4, right: 10, top: compact ? 10 : 18, bottom: 2, containLabel: true },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params;
        const row = rows[p.dataIndex];
        return `${row?.date ?? ""}<br/><b>${fmtToman(p.value || 0)}</b>`;
      },
      ...tooltip,
    },
    xAxis: {
      type: "category",
      data: rows.map((d) => fmtNum(d.day)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: alpha(theme.palette.text.secondary, 0.25) } },
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT, fontSize: compact ? 9 : 11,
        interval: compact ? Math.max(2, Math.floor(rows.length / 8)) : (rows.length > 16 ? 1 : 0),
      },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: theme.palette.text.secondary, fontFamily: FONT,
        fontSize: compact ? 9 : 11, formatter: fmtAxisToman,
      },
      splitLine: { lineStyle: { color: theme.palette.divider } },
    },
    series: [{
      type: "bar",
      data: rows.map((d) => d.amount_toman),
      barMaxWidth: compact ? 12 : 18,
      itemStyle: {
        borderRadius: [compact ? 4 : 6, compact ? 4 : 6, 0, 0],
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
