import { createTheme, PaletteMode, alpha } from "@mui/material/styles";

export function makeTheme(mode: PaletteMode) {
  const isDark = mode === "dark";
  const primaryMain = isDark ? "#7aa2e3" : "#22479c";
  const cardBorder = isDark ? "rgba(148,163,184,.14)" : "#eceff5";

  return createTheme({
    direction: "rtl",
    palette: {
      mode,
      primary: { main: primaryMain },
      secondary: { main: "#f29f05" },
      background: isDark
        ? { default: "#0b1220", paper: "#141c2e" }
        : { default: "#f5f7fb", paper: "#ffffff" },
      success: { main: "#16a34a" },
      error: { main: isDark ? "#f87171" : "#dc2626" },
      warning: { main: isDark ? "#fbbf24" : "#d97706" },
      info: { main: isDark ? "#38bdf8" : "#0284c7" },
      divider: isDark ? "rgba(148,163,184,.18)" : "#e8ebf1",
      text: isDark
        ? { primary: "#e8edf6", secondary: "#9fb0c9" }
        : { primary: "#1b2438", secondary: "#64748b" },
    },
    typography: {
      fontFamily: "Vazirmatn, system-ui, sans-serif",
      h4: { fontWeight: 800, letterSpacing: "-.02em" },
      h5: { fontWeight: 800, letterSpacing: "-.01em" },
      h6: { fontWeight: 800, letterSpacing: "-.01em" },
      subtitle1: { fontWeight: 700 },
      subtitle2: { fontWeight: 700 },
      button: { fontWeight: 700 },
    },
    shape: { borderRadius: 12 },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          "*": { scrollbarWidth: "thin", scrollbarColor: `${isDark ? "#33415580" : "#cbd5e1"} transparent` },
          "*::-webkit-scrollbar": { width: 9, height: 9 },
          "*::-webkit-scrollbar-thumb": {
            backgroundColor: isDark ? "#334155" : "#cbd5e1", borderRadius: 8,
            border: "2px solid transparent", backgroundClip: "content-box",
          },
          "*::-webkit-scrollbar-thumb:hover": { backgroundColor: isDark ? "#475569" : "#94a3b8" },
          body: { WebkitFontSmoothing: "antialiased" },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: { borderRadius: 10, textTransform: "none", fontWeight: 700, paddingInline: 16 },
          containedPrimary: {
            boxShadow: `0 2px 8px ${alpha(primaryMain, isDark ? 0.0 : 0.28)}`,
            "&:hover": { boxShadow: `0 4px 14px ${alpha(primaryMain, isDark ? 0.0 : 0.38)}` },
          },
          sizeLarge: { paddingBlock: 10 },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: 16,
            backgroundImage: "none",
            border: `1px solid ${cardBorder}`,
            boxShadow: isDark ? "none" : "0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06)",
          },
        },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            borderRadius: 10,
            transition: "box-shadow .15s, border-color .15s",
            "&:hover .MuiOutlinedInput-notchedOutline": { borderColor: isDark ? "#475569" : "#aab4c5" },
            "&.Mui-focused": { boxShadow: `0 0 0 3px ${alpha(primaryMain, 0.16)}` },
          },
        },
      },
      MuiAppBar: {
        styleOverrides: {
          root: {
            backdropFilter: "saturate(180%) blur(8px)",
            backgroundColor: isDark ? "rgba(20,28,46,.78)" : "rgba(255,255,255,.78)",
          },
        },
      },
      // Tinted, bold table headers + comfy rows — applies to every table in the app.
      MuiTableHead: {
        styleOverrides: {
          root: {
            "& .MuiTableCell-head": {
              backgroundColor: isDark ? "#1b2538" : "#f5f7fb",
              color: isDark ? "#cbd5e1" : "#475569",
              fontWeight: 700,
              fontSize: 12.5,
              letterSpacing: ".01em",
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
          root: {
            "&:last-child td": { borderBottom: 0 },
            "&.MuiTableRow-hover:hover": { backgroundColor: alpha(primaryMain, isDark ? 0.08 : 0.04) },
          },
        },
      },
      MuiTableSortLabel: { styleOverrides: { icon: { opacity: 0.5 } } },
      MuiChip: { styleOverrides: { root: { fontWeight: 700, borderRadius: 8 } } },
      MuiTab: { styleOverrides: { root: { textTransform: "none", fontWeight: 700, minHeight: 44 } } },
      MuiTabs: { styleOverrides: { indicator: { height: 3, borderRadius: 3 } } },
      MuiPaper: { styleOverrides: { root: { backgroundImage: "none" } } },
      MuiDialog: { styleOverrides: { paper: { borderRadius: 18 } } },
      MuiTooltip: {
        styleOverrides: {
          tooltip: { fontSize: 12, fontWeight: 600, borderRadius: 8, paddingBlock: 6, paddingInline: 10 },
        },
      },
      MuiListItemButton: { styleOverrides: { root: { borderRadius: 10 } } },
    },
  });
}

// Default (light) — kept for any direct import.
export const theme = makeTheme("light");

export const CHART_COLORS = [
  "#22479c", "#f29f05", "#16a34a", "#dc2626", "#7c3aed",
  "#0891b2", "#db2777", "#65a30d", "#ea580c", "#475569",
];
