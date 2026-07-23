import type { Theme } from "@mui/material/styles";

/**
 * Shared ECharts tooltip surface (D4, §5-G4): the glass Tooltip tokens + theme text,
 * so every chart tooltip matches the app's MUI tooltips in both modes. Spread into an
 * option's `tooltip` alongside the chart's own trigger/formatter.
 */
export function chartTooltip(theme: Theme) {
  const isDark = theme.palette.mode === "dark";
  return {
    backgroundColor: isDark ? "rgba(28,28,30,0.92)" : "rgba(255,255,255,0.88)",
    borderColor: isDark ? "rgba(255,255,255,0.14)" : "rgba(0,0,0,0.05)",
    textStyle: { color: theme.palette.text.primary, fontFamily: "Vazirmatn, sans-serif" },
  };
}
