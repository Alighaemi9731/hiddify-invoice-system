import { createTheme, PaletteMode } from "@mui/material/styles";

export function makeTheme(mode: PaletteMode) {
  const isDark = mode === "dark";
  return createTheme({
    direction: "rtl",
    palette: {
      mode,
      primary: { main: isDark ? "#7aa2e3" : "#1f3b73" },
      secondary: { main: "#f29f05" },
      background: isDark
        ? { default: "#0b1220", paper: "#141c2e" }
        : { default: "#f4f6fb", paper: "#ffffff" },
      success: { main: "#16a34a" },
      error: { main: isDark ? "#f87171" : "#dc2626" },
      warning: { main: "#d97706" },
      divider: isDark ? "rgba(148,163,184,.18)" : "#e5e7eb",
    },
    typography: {
      fontFamily: "Vazirmatn, system-ui, sans-serif",
      h6: { fontWeight: 700 },
    },
    shape: { borderRadius: 12 },
    components: {
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: { root: { borderRadius: 10, textTransform: "none", fontWeight: 600 } },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: 16,
            backgroundImage: "none",
            border: `1px solid ${isDark ? "rgba(148,163,184,.14)" : "#eef0f4"}`,
            boxShadow: isDark ? "none" : "0 1px 3px rgba(16,24,40,.06)",
          },
        },
      },
      // Tinted, bold table headers + comfy rows — applies to every table in the app.
      MuiTableHead: {
        styleOverrides: {
          root: {
            "& .MuiTableCell-head": {
              backgroundColor: isDark ? "#1b2538" : "#f5f7fb",
              color: isDark ? "#cbd5e1" : "#334155",
              fontWeight: 700,
              fontSize: 13,
              borderBottom: `2px solid ${isDark ? "rgba(148,163,184,.2)" : "#e5e7eb"}`,
              whiteSpace: "nowrap",
            },
          },
        },
      },
      MuiTableCell: {
        styleOverrides: { root: { borderColor: isDark ? "rgba(148,163,184,.12)" : "#eef0f4" } },
      },
      MuiTableRow: {
        styleOverrides: {
          root: { "&:last-child td": { borderBottom: 0 } },
        },
      },
      MuiTableSortLabel: {
        styleOverrides: { icon: { opacity: 0.5 } },
      },
      MuiChip: { styleOverrides: { root: { fontWeight: 600 } } },
      MuiTab: { styleOverrides: { root: { textTransform: "none", fontWeight: 600 } } },
      MuiPaper: { styleOverrides: { root: { backgroundImage: "none" } } },
    },
  });
}

// Default (light) — kept for any direct import.
export const theme = makeTheme("light");

export const CHART_COLORS = [
  "#1f3b73", "#f29f05", "#16a34a", "#dc2626", "#7c3aed",
  "#0891b2", "#db2777", "#65a30d", "#ea580c", "#475569",
];
