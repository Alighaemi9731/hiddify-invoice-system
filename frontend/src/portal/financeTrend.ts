import type { Theme } from "@mui/material/styles";
import { alpha } from "@mui/material/styles";
import { chartTooltip } from "../components/chartTooltip";
import { CHART_FONT } from "../themeTokens";
import { fmtNum, fmtToman } from "../format";

const FONT = CHART_FONT;
const faDigits = (s: string) => s.replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d]);

export interface FinanceRow {
  label: string;
  cost_toman: number;
  net_sales_toman: number;
  profit_toman: number;
}

// ECharts option for «روند ماهانه» on the storefront finance page: what the bot collected against
// what that quota costs on the owner's invoice, month by month. Mirrors monthlyTrend.ts / dailyTrend.ts
// so the portal's charts stay one system (gradient bars, shadow tooltip, dark/light aware). Oldest
// month on the left, so the bars read left-to-right as time even in an RTL page.
export function financeTrendOption(theme: Theme, rows: FinanceRow[]) {
  const isDark = theme.palette.mode === "dark";
  const received = theme.palette.primary.main;
  const cost = theme.palette.error.main;
  const fmtAxisToman = (v: number) =>
    v >= 1_000_000 ? `${fmtNum(Math.round(v / 1_000_000))}م`
      : v >= 1_000 ? `${fmtNum(Math.round(v / 1_000))}هزار` : fmtNum(v);
  const bar = (color: string, values: number[]) => ({
    barMaxWidth: 26,
    itemStyle: {
      borderRadius: [6, 6, 0, 0],
      color: {
        type: "linear", x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [
          { offset: 0, color },
          { offset: 1, color: alpha(color, isDark ? 0.35 : 0.45) },
        ],
      },
    },
    data: values,
  });
  return {
    textStyle: { fontFamily: FONT },
    grid: { left: 4, right: 10, top: 34, bottom: 2, containLabel: true },
    legend: {
      top: 0,
      itemHeight: 9,
      itemWidth: 14,
      textStyle: { color: theme.palette.text.secondary, fontFamily: FONT, fontSize: 12 },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params: any) => {
        const first = Array.isArray(params) ? params[0] : params;
        const row = rows[first?.dataIndex];
        if (!row) return "";
        return `${faDigits(row.label)}<br/>دریافتی: <b>${fmtToman(row.net_sales_toman)}</b>`
          + `<br/>هزینه: ${fmtToman(row.cost_toman)}`
          + `<br/>سود: <b>${fmtToman(row.profit_toman)}</b>`;
      },
      ...chartTooltip(theme),
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
    series: [
      { name: "دریافتی", type: "bar", ...bar(received, rows.map((d) => d.net_sales_toman)) },
      { name: "هزینه", type: "bar", ...bar(cost, rows.map((d) => d.cost_toman)) },
    ],
  };
}
