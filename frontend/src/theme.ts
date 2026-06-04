import { createTheme, PaletteMode, alpha } from "@mui/material/styles";

/**
 * Design language: modern "fintech" — a refined indigo-violet accent on sophisticated
 * slate neutrals, soft depth, subtle glass. Dark mode is deep slate-violet (never pure
 * black); light mode is a soft cool off-white. Both share the same accent so the brand
 * feels consistent across modes.
 */
export function makeTheme(mode: PaletteMode) {
  const isDark = mode === "dark";

  // Accent — indigo-violet. A deeper indigo on light for crisp contrast; a soft lavender
  // on dark so it glows against the slate background (the 2026 dashboard look).
  const primaryMain = isDark ? "#a78bfa" : "#5b50e6";
  const cardBorder = isDark ? "rgba(148,163,184,.12)" : "#ececf4";
  const cardShadow = isDark
    ? "0 1px 2px rgba(0,0,0,.35), 0 1px 3px rgba(0,0,0,.2)"
    : "0 1px 2px rgba(16,24,40,.04), 0 4px 16px -8px rgba(16,24,40,.10)";

  return createTheme({
    direction: "rtl",
    palette: {
      mode,
      primary: { main: primaryMain },
      secondary: { main: isDark ? "#fbbf24" : "#f59e0b" },
      background: isDark
        ? { default: "#0d0f1a", paper: "#161a29" }
        : { default: "#f5f6fc", paper: "#ffffff" },
      success: { main: isDark ? "#34d399" : "#16a34a" },
      error: { main: isDark ? "#fb7185" : "#e11d48" },
      warning: { main: isDark ? "#fbbf24" : "#d97706" },
      info: { main: isDark ? "#38bdf8" : "#0284c7" },
      divider: isDark ? "rgba(148,163,184,.13)" : "#e8e9f2",
      text: isDark
        ? { primary: "#e8eaf4", secondary: "#969cb3" }
        : { primary: "#1b1d2e", secondary: "#6b7188" },
    },
    typography: {
      fontFamily: "Vazirmatn, system-ui, sans-serif",
      h4: { fontWeight: 800, letterSpacing: "-.02em" },
      h5: { fontWeight: 800, letterSpacing: "-.015em" },
      h6: { fontWeight: 800, letterSpacing: "-.01em" },
      subtitle1: { fontWeight: 700 },
      subtitle2: { fontWeight: 700 },
      button: { fontWeight: 700 },
    },
    shape: { borderRadius: 12 },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          "*": { scrollbarWidth: "thin", scrollbarColor: `${isDark ? "#2a3147" : "#cdd2e3"} transparent` },
          "*::-webkit-scrollbar": { width: 9, height: 9 },
          "*::-webkit-scrollbar-thumb": {
            backgroundColor: isDark ? "#2a3147" : "#cdd2e3", borderRadius: 8,
            border: "2px solid transparent", backgroundClip: "content-box",
          },
          "*::-webkit-scrollbar-thumb:hover": { backgroundColor: isDark ? "#3a4262" : "#aeb4cc" },
          body: { WebkitFontSmoothing: "antialiased" },
          "::selection": { backgroundColor: alpha(primaryMain, 0.28) },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: { borderRadius: 10, textTransform: "none", fontWeight: 700, paddingInline: 16 },
          containedPrimary: {
            boxShadow: isDark
              ? "none"
              : `0 1px 2px ${alpha(primaryMain, 0.24)}, 0 8px 18px -8px ${alpha(primaryMain, 0.55)}`,
            "&:hover": {
              boxShadow: isDark
                ? `0 0 0 1px ${alpha(primaryMain, 0.5)}`
                : `0 2px 4px ${alpha(primaryMain, 0.28)}, 0 12px 22px -8px ${alpha(primaryMain, 0.62)}`,
            },
          },
          sizeLarge: { paddingBlock: 10 },
          outlined: { borderColor: alpha(primaryMain, isDark ? 0.4 : 0.35) },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: 16, backgroundImage: "none",
            border: `1px solid ${cardBorder}`, boxShadow: cardShadow,
          },
        },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            borderRadius: 10, transition: "box-shadow .15s, border-color .15s",
            "&:hover .MuiOutlinedInput-notchedOutline": { borderColor: isDark ? "#475569" : "#b3b8cc" },
            "&.Mui-focused": { boxShadow: `0 0 0 3px ${alpha(primaryMain, 0.18)}` },
            "&.Mui-focused .MuiOutlinedInput-notchedOutline": { borderWidth: 1 },
          },
        },
      },
      MuiAppBar: {
        styleOverrides: {
          root: {
            backdropFilter: "saturate(180%) blur(10px)",
            backgroundColor: isDark ? "rgba(13,15,26,.72)" : "rgba(255,255,255,.72)",
            color: isDark ? "#e8eaf4" : "#1b1d2e",
          },
        },
      },
      MuiTableHead: {
        styleOverrides: {
          root: {
            "& .MuiTableCell-head": {
              backgroundColor: isDark ? "#1b2030" : "#f6f7fc",
              color: isDark ? "#aab0c6" : "#5a6173",
              fontWeight: 700, fontSize: 12.5, letterSpacing: ".01em",
              borderBottom: `1px solid ${isDark ? "rgba(148,163,184,.16)" : "#e8e9f2"}`,
              whiteSpace: "nowrap",
            },
          },
        },
      },
      MuiTableCell: {
        styleOverrides: { root: { borderColor: isDark ? "rgba(148,163,184,.10)" : "#eef0f6" } },
      },
      MuiTableRow: {
        styleOverrides: {
          root: {
            "&:last-child td": { borderBottom: 0 },
            "&.MuiTableRow-hover:hover": { backgroundColor: alpha(primaryMain, isDark ? 0.09 : 0.045) },
          },
        },
      },
      MuiTableSortLabel: { styleOverrides: { icon: { opacity: 0.5 } } },
      MuiChip: {
        styleOverrides: {
          root: { fontWeight: 700, borderRadius: 8 },
          outlined: { borderColor: isDark ? "rgba(148,163,184,.28)" : "#dfe2ee" },
        },
      },
      MuiTab: { styleOverrides: { root: { textTransform: "none", fontWeight: 700, minHeight: 44 } } },
      MuiTabs: { styleOverrides: { indicator: { height: 3, borderRadius: 3 } } },
      MuiPaper: { styleOverrides: { root: { backgroundImage: "none" } } },
      MuiDialog: { styleOverrides: { paper: { borderRadius: 18 } } },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            fontSize: 12, fontWeight: 600, borderRadius: 8, paddingBlock: 6, paddingInline: 10,
            backgroundColor: isDark ? "#2a3047" : "#1b1d2e",
          },
          arrow: { color: isDark ? "#2a3047" : "#1b1d2e" },
        },
      },
      MuiListItemButton: { styleOverrides: { root: { borderRadius: 10 } } },
      MuiSwitch: {
        styleOverrides: {
          root: { padding: 8 },
          track: { borderRadius: 11, opacity: isDark ? 0.4 : 0.3 },
        },
      },
      MuiLinearProgress: { styleOverrides: { root: { borderRadius: 6, height: 8 } } },
    },
  });
}

// Default (light) — kept for any direct import.
export const theme = makeTheme("light");

// Chart palette — leads with the indigo-violet accent, then a balanced modern sequence.
export const CHART_COLORS = [
  "#6d5efc", "#0ea5e9", "#22c55e", "#f59e0b", "#f43f5e",
  "#14b8a6", "#a855f7", "#84cc16", "#fb923c", "#64748b",
];
