import { createTheme, PaletteMode, alpha } from "@mui/material/styles";

/**
 * Design language: Apple "Liquid Glass" — translucent frosted surfaces that float over a
 * soft ambient color field, with specular edge highlights (a bright top rim + inset gloss)
 * and depth shadows. The colored ambient background is essential: frosted glass needs
 * something vivid behind it to tint and refract. Accent stays indigo-violet.
 *
 * We use high-fidelity glassmorphism (backdrop-filter + layered rim shadows) rather than
 * SVG displacement refraction, so it renders well and fast across all browsers.
 */
export function makeTheme(mode: PaletteMode) {
  const isDark = mode === "dark";
  const primaryMain = isDark ? "#a78bfa" : "#5b50e6";

  // Glass surface tokens.
  const glassBg = isDark ? "rgba(22,26,43,0.55)" : "rgba(255,255,255,0.60)";
  const glassBorder = isDark ? "rgba(255,255,255,0.10)" : "rgba(255,255,255,0.65)";
  const glassBlur = "blur(20px) saturate(180%)";
  // Drop shadow + bright top rim (the "specular highlight") + faint inner edge.
  const glassShadow = isDark
    ? "0 10px 34px -14px rgba(0,0,0,.72), inset 0 1px 0 rgba(255,255,255,.10), inset 0 0 0 1px rgba(255,255,255,.03)"
    : "0 10px 34px -16px rgba(31,38,80,.28), 0 2px 8px -4px rgba(31,38,80,.10), inset 0 1px 0 rgba(255,255,255,.85)";

  // Ambient background — layered radial color blobs over a deep/cool base, fixed so it
  // doesn't scroll. Kept low-saturation so dense tables stay readable through the glass.
  const ambient = isDark
    ? "radial-gradient(62% 52% at 0% 0%, rgba(124,108,255,.30), transparent 62%)," +
      "radial-gradient(58% 48% at 100% 0%, rgba(56,189,248,.22), transparent 62%)," +
      "radial-gradient(62% 56% at 100% 100%, rgba(168,139,250,.24), transparent 62%)," +
      "radial-gradient(54% 52% at 0% 100%, rgba(16,185,129,.16), transparent 62%)"
    : "radial-gradient(62% 52% at 0% 0%, rgba(124,108,255,.28), transparent 60%)," +
      "radial-gradient(58% 48% at 100% 0%, rgba(14,165,233,.22), transparent 60%)," +
      "radial-gradient(62% 56% at 100% 100%, rgba(244,63,94,.16), transparent 60%)," +
      "radial-gradient(54% 52% at 0% 100%, rgba(16,185,129,.18), transparent 60%)";

  // Staggered fade-in for table body rows — gives every data page the same "alive"
  // entrance the dashboard cards have, applied globally (no per-page wiring). Capped at
  // the first ~14 rows; later rows just fade in together.
  const rowStagger: Record<string, { animationDelay: string }> = {};
  for (let i = 1; i <= 14; i++) {
    rowStagger[`.MuiTableBody-root .MuiTableRow-root:nth-of-type(${i})`] = {
      animationDelay: `${i * 28}ms`,
    };
  }

  return createTheme({
    direction: "rtl",
    palette: {
      mode,
      primary: { main: primaryMain },
      secondary: { main: isDark ? "#fbbf24" : "#f59e0b" },
      background: isDark
        ? { default: "#090a12", paper: glassBg }
        : { default: "#e9ecf7", paper: glassBg },
      success: { main: isDark ? "#34d399" : "#16a34a" },
      error: { main: isDark ? "#fb7185" : "#e11d48" },
      warning: { main: isDark ? "#fbbf24" : "#d97706" },
      info: { main: isDark ? "#38bdf8" : "#0284c7" },
      divider: isDark ? "rgba(148,163,184,.14)" : "rgba(120,130,170,.18)",
      text: isDark
        ? { primary: "#e8eaf4", secondary: "#9aa0b6" }
        : { primary: "#1b1d2e", secondary: "#5a6175" },
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
    shape: { borderRadius: 14 },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          "*": { scrollbarWidth: "thin", scrollbarColor: `${isDark ? "#2a3147" : "#c4cae0"} transparent` },
          "*::-webkit-scrollbar": { width: 9, height: 9 },
          "*::-webkit-scrollbar-thumb": {
            backgroundColor: isDark ? "#2a3147" : "#c4cae0", borderRadius: 8,
            border: "2px solid transparent", backgroundClip: "content-box",
          },
          "*::-webkit-scrollbar-thumb:hover": { backgroundColor: isDark ? "#3a4262" : "#aab1ce" },
          html: { height: "100%" },
          body: {
            WebkitFontSmoothing: "antialiased",
            minHeight: "100%",
            backgroundColor: isDark ? "#090a12" : "#e9ecf7",
            backgroundImage: ambient,
            backgroundAttachment: "fixed",
            // Static gradient — no infinite animation (a moving full-viewport background
            // forces a per-frame repaint and was pegging the GPU / heating the machine).
          },
          // Respect users who prefer less motion.
          "@media (prefers-reduced-motion: reduce)": {
            "*": { animationDuration: "0.001ms !important", transitionDuration: "0.001ms !important" },
          },
          // Site-wide table-row entrance (fade + tiny rise), staggered.
          "@keyframes rowIn": {
            from: { opacity: 0, transform: "translateY(5px)" },
            to: { opacity: 1, transform: "translateY(0)" },
          },
          ".MuiTableBody-root .MuiTableRow-root": {
            animation: "rowIn .45s cubic-bezier(.22,1,.36,1) both",
          },
          ...rowStagger,
          // Responsive tables → cards on phones. A <Table className="resp-table"> collapses
          // each body row into a stacked "label: value" card below the sm breakpoint. Each
          // body cell carries data-label="<column>"; the header is hidden on mobile.
          "@media (max-width:599.95px)": {
            ".resp-table thead": { display: "none" },
            ".resp-table, .resp-table tbody, .resp-table tr, .resp-table td": {
              display: "block", width: "100%",
            },
            ".resp-table tr": {
              marginBottom: 10, borderRadius: 14, padding: "2px 12px",
              border: `1px solid ${glassBorder}`,
              backgroundColor: isDark ? "rgba(28,34,52,.45)" : "rgba(255,255,255,.5)",
            },
            ".resp-table tr:nth-of-type(even)": {
              backgroundColor: isDark ? "rgba(28,34,52,.45)" : "rgba(255,255,255,.5)",
            },
            ".resp-table td": {
              display: "flex", alignItems: "center", justifyContent: "space-between",
              gap: 12, textAlign: "start", minHeight: 42, minWidth: 0, overflow: "hidden",
              padding: "9px 0 !important",
              borderBottom: `1px solid ${isDark ? "rgba(148,163,184,.09)" : "rgba(120,130,170,.11)"} !important`,
            },
            // the value side may shrink/clip so a long cell never pushes the card wider
            ".resp-table td > *": { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" },
            ".resp-table td:last-child": { borderBottom: "0 !important" },
            ".resp-table td::before": {
              content: "attr(data-label)", fontWeight: 700, fontSize: 12.5,
              color: isDark ? "#9aa0b6" : "#5a6175", whiteSpace: "nowrap", flexShrink: 0,
            },
            ".resp-table td[data-label='']::before": { content: '""' },
          },
          "::selection": { backgroundColor: alpha(primaryMain, 0.28) },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: {
            borderRadius: 12, textTransform: "none", fontWeight: 700, paddingInline: 18,
            transition: "transform .14s cubic-bezier(.34,1.56,.64,1), box-shadow .2s ease, background-color .2s",
            "&:hover": { transform: "translateY(-1px)" },
            "&:active": { transform: "translateY(0) scale(.97)" },
          },
          containedPrimary: {
            // glossy top highlight, like a glass pill
            boxShadow: isDark
              ? "inset 0 1px 0 rgba(255,255,255,.22)"
              : `0 6px 16px -8px ${alpha(primaryMain, 0.7)}, inset 0 1px 0 rgba(255,255,255,.38)`,
            "&:hover": {
              boxShadow: isDark
                ? "inset 0 1px 0 rgba(255,255,255,.3), 0 0 0 1px rgba(255,255,255,.12)"
                : `0 10px 22px -8px ${alpha(primaryMain, 0.78)}, inset 0 1px 0 rgba(255,255,255,.45)`,
            },
          },
          outlined: {
            borderColor: alpha(primaryMain, isDark ? 0.45 : 0.35),
            backdropFilter: glassBlur,
            backgroundColor: isDark ? "rgba(255,255,255,.04)" : "rgba(255,255,255,.4)",
          },
          text: { "&:hover": { backgroundColor: alpha(primaryMain, isDark ? 0.12 : 0.07) } },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: 18, backgroundImage: "none",
            border: `1px solid ${glassBorder}`,
            backdropFilter: glassBlur, WebkitBackdropFilter: glassBlur,
            boxShadow: glassShadow,
          },
        },
      },
      // All Paper-based surfaces (menus, popovers, drawers, accordions) become glass.
      MuiPaper: {
        styleOverrides: {
          root: { backgroundImage: "none" },
          elevation0: { boxShadow: "none" },
        },
      },
      MuiMenu: {
        styleOverrides: {
          paper: {
            backdropFilter: glassBlur, WebkitBackdropFilter: glassBlur,
            border: `1px solid ${glassBorder}`, borderRadius: 14, boxShadow: glassShadow,
          },
        },
      },
      MuiPopover: { styleOverrides: { paper: { backdropFilter: glassBlur, WebkitBackdropFilter: glassBlur } } },
      MuiDialog: {
        styleOverrides: {
          paper: {
            borderRadius: 22, border: `1px solid ${glassBorder}`,
            backdropFilter: "blur(28px) saturate(190%)", WebkitBackdropFilter: "blur(28px) saturate(190%)",
            backgroundColor: isDark ? "rgba(22,26,43,0.78)" : "rgba(255,255,255,0.80)",
            boxShadow: glassShadow,
          },
        },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            borderRadius: 12, transition: "box-shadow .15s, border-color .15s, background-color .15s",
            backgroundColor: isDark ? "rgba(255,255,255,.03)" : "rgba(255,255,255,.45)",
            "&:hover": { backgroundColor: isDark ? "rgba(255,255,255,.05)" : "rgba(255,255,255,.6)" },
            "&:hover .MuiOutlinedInput-notchedOutline": { borderColor: isDark ? "#475569" : "#aeb4cc" },
            "&.Mui-focused": {
              backgroundColor: isDark ? "rgba(255,255,255,.05)" : "rgba(255,255,255,.7)",
              boxShadow: `0 0 0 3px ${alpha(primaryMain, 0.18)}`,
            },
          },
        },
      },
      MuiAppBar: {
        styleOverrides: {
          root: {
            backdropFilter: "saturate(180%) blur(18px)", WebkitBackdropFilter: "saturate(180%) blur(18px)",
            backgroundColor: isDark ? "rgba(9,10,18,.62)" : "rgba(255,255,255,.62)",
            color: isDark ? "#e8eaf4" : "#1b1d2e",
          },
        },
      },
      MuiTableHead: {
        styleOverrides: {
          root: {
            "& .MuiTableCell-head": {
              backgroundColor: isDark ? alpha(primaryMain, 0.16) : alpha(primaryMain, 0.08),
              color: isDark ? "#c9bffb" : "#4b3fb5",
              fontWeight: 800, fontSize: 12.5, letterSpacing: ".01em",
              borderBottom: `1px solid ${alpha(primaryMain, isDark ? 0.3 : 0.22)}`,
              whiteSpace: "nowrap", backdropFilter: "blur(6px)",
            },
          },
        },
      },
      MuiTableCell: {
        styleOverrides: { root: { borderColor: isDark ? "rgba(148,163,184,.10)" : "rgba(120,130,170,.12)" } },
      },
      MuiTableRow: {
        styleOverrides: {
          root: {
            transition: "background-color .15s ease",
            "&:last-child td": { borderBottom: 0 },
            // subtle accent zebra striping → less flat/grey, easier to scan
            "&:nth-of-type(even)": { backgroundColor: alpha(primaryMain, isDark ? 0.04 : 0.035) },
            "&.MuiTableRow-hover:hover": { backgroundColor: alpha(primaryMain, isDark ? 0.13 : 0.08) },
          },
        },
      },
      MuiTableSortLabel: { styleOverrides: { icon: { opacity: 0.5 } } },
      MuiChip: {
        styleOverrides: {
          root: { fontWeight: 700, borderRadius: 9 },
          outlined: {
            borderColor: isDark ? "rgba(148,163,184,.28)" : "rgba(120,130,170,.28)",
            backgroundColor: isDark ? "rgba(255,255,255,.03)" : "rgba(255,255,255,.35)",
            backdropFilter: "blur(8px)",
          },
        },
      },
      MuiTab: { styleOverrides: { root: { textTransform: "none", fontWeight: 700, minHeight: 44 } } },
      MuiTabs: { styleOverrides: { indicator: { height: 3, borderRadius: 3 } } },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            fontSize: 12, fontWeight: 600, borderRadius: 9, paddingBlock: 6, paddingInline: 10,
            backgroundColor: isDark ? "rgba(42,48,71,.92)" : "rgba(27,29,46,.92)",
            backdropFilter: "blur(10px)", border: `1px solid ${glassBorder}`,
          },
          arrow: { color: isDark ? "rgba(42,48,71,.92)" : "rgba(27,29,46,.92)" },
        },
      },
      MuiIconButton: {
        styleOverrides: {
          root: {
            transition: "transform .15s cubic-bezier(.34,1.56,.64,1), background-color .2s, color .2s",
            "&:hover": { transform: "scale(1.12)" },
            "&:active": { transform: "scale(.94)" },
          },
        },
      },
      MuiListItemButton: { styleOverrides: { root: { borderRadius: 12, transition: "background-color .18s ease" } } },
      MuiSwitch: { styleOverrides: { root: { padding: 8 }, track: { borderRadius: 11, opacity: isDark ? 0.4 : 0.3 } } },
      MuiLinearProgress: { styleOverrides: { root: { borderRadius: 6, height: 8 } } },
      MuiSkeleton: {
        defaultProps: { animation: "wave" },
        styleOverrides: {
          root: {
            backgroundColor: isDark ? "rgba(255,255,255,.06)" : "rgba(120,130,170,.11)",
            "&::after": {
              background: `linear-gradient(90deg, transparent, ${
                isDark ? "rgba(255,255,255,.07)" : "rgba(255,255,255,.6)"
              }, transparent)`,
            },
          },
        },
      },
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
