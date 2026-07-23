import type { PaletteMode } from "@mui/material";
import { createTheme, alpha } from "@mui/material/styles";
import { CHROME_BLUR, CHROME_SIDEBAR_BG, CHROME_SIDEBAR_BORDER, TIER2_BLUR } from "./themeTokens";

// Minimal frosted-noise SVG — micro-texture on glass surfaces.
const NOISE =
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' " +
  "opacity='0.03'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' " +
  "baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C" +
  "feColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect " +
  "width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")";

export function makeTheme(mode: PaletteMode) {
  const isDark = mode === "dark";

  // Apple system blue — exact values from apple.com computed styles
  const primaryMain = isDark ? "#2997ff" : "#0071e3";

  // ── Glass tokens — sourced from apple.com ─────────────────────────────────
  // apple.com pill bg: rgba(255,255,255,0.1)
  // apple.com localnav dark: rgba(0,0,0,0.6) + blur(~40px)
  // apple.com pill border: rgba(217,207,207,0.25)

  // Tier-1: content surfaces. Dark cards are NEAR-OPAQUE (#1c1c1e @ 90%): stacking a second
  // translucency (mobile row-cards) on a 7%-white surface + blur(40px) produced a washed-out
  // "haze" that made text hard to read. Glass identity is kept via the reduced-blur edge + the
  // top specular rim; sidebar/AppBar/dialogs keep the FULL translucent glass (separate tokens).
  const glassBg     = isDark ? "rgba(28,28,30,0.90)" : "rgba(255,255,255,0.78)";
  const glassBorder = isDark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.05)";
  const glassBlur   = isDark ? "blur(20px) saturate(140%)" : "blur(40px) saturate(180%)";

  // Tier-2: floating overlays (Apple nav/dialog style)
  const floatBg   = isDark ? "rgba(28,28,30,0.82)"  : "rgba(255,255,255,0.88)";
  const floatBlur = TIER2_BLUR;

  // Apple uses minimal shadows — border defines the element, not a heavy drop
  const glassShadow = isDark
    ? [
        "0 0 0 0.5px rgba(255,255,255,0.10)",
        "0 2px 20px rgba(0,0,0,0.50)",
        "inset 0 1px 0 rgba(255,255,255,0.08)",
      ].join(", ")
    : [
        "0 0 0 0.5px rgba(0,0,0,0.05)",
        "0 2px 20px rgba(0,0,0,0.06)",
        "inset 0 1px 0 rgba(255,255,255,1.0)",
      ].join(", ");

  const floatShadow = isDark
    ? [
        "0 0 0 0.5px rgba(255,255,255,0.14)",
        "0 12px 40px rgba(0,0,0,0.70)",
        "inset 0 1px 0 rgba(255,255,255,0.10)",
      ].join(", ")
    : [
        "0 0 0 0.5px rgba(0,0,0,0.08)",
        "0 12px 40px rgba(0,0,0,0.14)",
        "inset 0 1px 0 rgba(255,255,255,1.0)",
      ].join(", ");

  // Apple's very restrained top-light gradient
  const glassInner = isDark
    ? "linear-gradient(175deg,rgba(255,255,255,0.05) 0%,rgba(255,255,255,0) 50%)"
    : "linear-gradient(175deg,rgba(255,255,255,0.70) 0%,rgba(255,255,255,0) 50%)";

  // In dark mode: only noise (no white gradient → eliminates the "foggy" tint on black)
  // In light mode: noise + top-light gloss
  const glassBgImage = isDark ? NOISE : `${NOISE}, ${glassInner}`;

  // Tier-1 mixin
  const glassSurface = {
    backdropFilter:       glassBlur,
    WebkitBackdropFilter: glassBlur,
    backgroundColor:      glassBg,
    backgroundImage:      glassBgImage,
    border:               `1px solid ${glassBorder}`,
    boxShadow:            glassShadow,
  };

  // Tier-2 mixin
  const floatSurface = {
    backdropFilter:       floatBlur,
    WebkitBackdropFilter: floatBlur,
    backgroundColor:      floatBg,
    backgroundImage:      glassBgImage,
    border:               `1px solid ${glassBorder}`,
    boxShadow:            floatShadow,
  };

  // ── Ambient background — Apple style: clean, one subtle glow ─────────────
  // Apple uses pure black/white — no colored blobs. One very faint brand glow.
  const ambient = isDark
    ? "radial-gradient(100% 40% at 50% 0%, rgba(41,151,255,0.06), transparent 60%)"
    : "radial-gradient(100% 40% at 50% 0%, rgba(0,113,227,0.025), transparent 60%)";

  // ── Row entrance stagger ──────────────────────────────────────────────────
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
      primary:   { main: primaryMain },
      secondary: { main: isDark ? "#ff9f0a" : "#ff9500" },   // Apple orange
      background: isDark
        ? { default: "#000000", paper: glassBg }
        : { default: "#f5f5f7", paper: glassBg },
      success: { main: isDark ? "#30d158" : "#28cd41" },     // Apple green
      error:   { main: isDark ? "#ff453a" : "#ff3b30" },     // Apple red
      warning: { main: isDark ? "#ffd60a" : "#ff9500" },     // Apple yellow
      info:    { main: isDark ? "#2997ff" : "#0071e3" },     // Apple blue
      divider: isDark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.08)",
      text: isDark
        ? { primary: "#f5f5f7", secondary: "#a1a1a6" }       // secondary lifted for ≈6.6:1 on #1c1c1e
        : { primary: "#1d1d1f", secondary: "#6e6e73" },      // Apple light text
    },
    typography: {
      fontFamily: "Vazirmatn, system-ui, -apple-system, sans-serif",
      h4: { fontWeight: 700, letterSpacing: "-.02em" },
      h5: { fontWeight: 700, letterSpacing: "-.015em" },
      h6: { fontWeight: 700, letterSpacing: "-.01em" },
      subtitle1: { fontWeight: 600 },
      subtitle2: { fontWeight: 600 },
      button:    { fontWeight: 600 },
    },
    shape: { borderRadius: 14 },

    components: {
      // ── Global baseline ───────────────────────────────────────────────
      MuiCssBaseline: {
        styleOverrides: {
          "*": { scrollbarWidth: "thin", scrollbarColor: `${isDark ? "rgba(255,255,255,.14)" : "rgba(0,0,0,.12)"} transparent` },
          "*::-webkit-scrollbar": { width: 8, height: 8 },
          "*::-webkit-scrollbar-thumb": {
            backgroundColor: isDark ? "rgba(255,255,255,.14)" : "rgba(0,0,0,.12)",
            borderRadius: 8, border: "2px solid transparent", backgroundClip: "content-box",
          },
          "*::-webkit-scrollbar-thumb:hover": {
            backgroundColor: isDark ? "rgba(255,255,255,.22)" : "rgba(0,0,0,.20)",
          },
          html: { height: "100%" },
          body: {
            WebkitFontSmoothing: "antialiased",
            MozOsxFontSmoothing: "grayscale",
            minHeight: "100%",
            backgroundColor: isDark ? "#000000" : "#f5f5f7",
          },
          "body::before": {
            content: '""',
            position: "fixed",
            inset: 0,
            zIndex: -1,
            backgroundImage: ambient,
            pointerEvents: "none",
          },

          "@keyframes rowIn": {
            from: { opacity: 0, transform: "translateY(6px)" },
            to:   { opacity: 1, transform: "translateY(0)" },
          },
          "@keyframes glassIn": {
            from: { opacity: 0, transform: "scale(.97) translateY(8px)" },
            to:   { opacity: 1, transform: "scale(1) translateY(0)" },
          },

          ".MuiTableBody-root .MuiTableRow-root": {
            animation: "rowIn .36s cubic-bezier(.22,1,.36,1) both",
          },
          ...rowStagger,

          // Mobile responsive card-tables
          "@media (max-width:599.95px)": {
            ".resp-table thead": { display: "none" },
            ".resp-table, .resp-table tbody, .resp-table tr, .resp-table td": {
              display: "block", width: "100%",
            },
            ".resp-table tr": {
              marginBottom: 10,
              borderRadius: 14,
              padding: "2px 12px",
              border: `1px solid ${glassBorder}`,
              // Near-opaque in dark (same haze fix as the explicit mobile row-cards); reduced blur.
              backgroundColor: isDark ? "rgba(36,36,38,0.94)" : "rgba(255,255,255,0.72)",
              backdropFilter: isDark ? "blur(20px) saturate(140%)" : "blur(40px) saturate(180%)",
              WebkitBackdropFilter: isDark ? "blur(20px) saturate(140%)" : "blur(40px) saturate(180%)",
            },
            ".resp-table td": {
              display: "flex", alignItems: "center", justifyContent: "space-between",
              gap: 12, textAlign: "start", minHeight: 42, minWidth: 0, overflow: "hidden",
              padding: "9px 0 !important",
              borderBottom: `1px solid ${isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)"} !important`,
            },
            ".resp-table td > *": { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" },
            ".resp-table td:last-child": { borderBottom: "0 !important" },
            ".resp-table td::before": {
              content: "attr(data-label)", fontWeight: 600, fontSize: 12.5,
              color: isDark ? "#a1a1a6" : "#6e6e73", whiteSpace: "nowrap", flexShrink: 0,
            },
            ".resp-table td[data-label='']::before": { content: '""' },
          },

          "@media (prefers-reduced-motion: reduce)": {
            "*": { animationDuration: "0.001ms !important", transitionDuration: "0.001ms !important" },
          },
          "::selection": { backgroundColor: alpha(primaryMain, 0.28) },
          ":focus-visible": {
            outline: `2px solid ${alpha(primaryMain, 0.80)}`,
            outlineOffset: 2, borderRadius: 4,
          },
          "a:focus:not(:focus-visible), button:focus:not(:focus-visible)": { outline: "none" },
        },
      },

      // ── Surfaces ──────────────────────────────────────────────────────
      MuiCard: {
        styleOverrides: {
          root: {
            ...glassSurface,
            borderRadius: 18,
            transition: "box-shadow .20s, transform .20s",
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          root: { backgroundImage: "none" },
          elevation0: { boxShadow: "none" },
        },
      },
      MuiMenu: {
        styleOverrides: {
          paper: { ...floatSurface, borderRadius: 14 },
        },
      },
      MuiPopover: {
        styleOverrides: {
          paper: { ...floatSurface, borderRadius: 14 },
        },
      },
      MuiDialog: {
        styleOverrides: {
          paper: {
            ...floatSurface,
            borderRadius: 20,
            "&.MuiDialog-paperFullScreen": { borderRadius: 0 },
          },
        },
      },
      MuiBackdrop: {
        styleOverrides: {
          root: { backgroundColor: isDark ? "rgba(0,0,0,0.50)" : "rgba(0,0,0,0.20)" },
        },
      },
      // Mobile drawer = the same chrome glass as the desktop sidebar, so the one
      // sidebar renders identically on both form factors.
      MuiDrawer: {
        styleOverrides: {
          paper: {
            backdropFilter: CHROME_BLUR,
            WebkitBackdropFilter: CHROME_BLUR,
            backgroundColor: isDark ? CHROME_SIDEBAR_BG.dark : CHROME_SIDEBAR_BG.light,
            backgroundImage: glassBgImage,
            borderInlineStart: `1px solid ${isDark ? CHROME_SIDEBAR_BORDER.dark : CHROME_SIDEBAR_BORDER.light}`,
          },
        },
      },
      // Apple nav bar: rgba(0,0,0,0.6) dark / rgba(255,255,255,0.8) light + blur(40px)
      MuiAppBar: {
        styleOverrides: {
          root: {
            backdropFilter: "blur(40px) saturate(180%)",
            WebkitBackdropFilter: "blur(40px) saturate(180%)",
            backgroundColor: isDark ? "rgba(0,0,0,0.60)" : "rgba(255,255,255,0.80)",
            backgroundImage: `${NOISE}`,
            color: isDark ? "#f5f5f7" : "#1d1d1f",
            boxShadow: isDark
              ? "0 0 0 0.5px rgba(255,255,255,0.08)"
              : "0 0 0 0.5px rgba(0,0,0,0.08)",
          },
        },
      },

      // ── Select ────────────────────────────────────────────────────────
      MuiSelect: {
        styleOverrides: {
          select: {
            paddingBlock: "7px",
            paddingInlineStart: "16px",
            minHeight: "unset",
            display: "flex",
            alignItems: "center",
          },
          icon: {
            color: isDark ? "rgba(255,255,255,0.35)" : "rgba(0,0,0,0.28)",
            transition: "transform .18s",
          },
        },
      },

      // ── Inputs ────────────────────────────────────────────────────────
      MuiOutlinedInput: {
        styleOverrides: {
          input: {
            textOverflow: "ellipsis",
            overflow: "hidden",
            whiteSpace: "nowrap",
          },
          multiline: {
            // textarea rows need moderate rounding, not full pill
            borderRadius: "14px",
          },
          root: {
            // 50px = pill for small inputs (36-40px tall), proportional rounding for taller ones
            borderRadius: "50px",
            transition: "box-shadow .15s, border-color .15s, background-color .2s",
            backgroundColor: isDark ? "rgba(255,255,255,0.06)" : "rgba(255,255,255,0.80)",
            "&:hover": {
              backgroundColor: isDark ? "rgba(255,255,255,0.09)" : "rgba(255,255,255,0.95)",
            },
            "&:hover .MuiOutlinedInput-notchedOutline": {
              borderColor: isDark ? "rgba(255,255,255,0.22)" : "rgba(0,0,0,0.18)",
            },
            "&.Mui-focused": {
              backgroundColor: isDark ? "rgba(255,255,255,0.08)" : "rgba(255,255,255,1.0)",
              boxShadow: `0 0 0 3px ${alpha(primaryMain, 0.22)}`,
            },
          },
          notchedOutline: {
            borderColor: isDark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.12)",
          },
        },
      },

      // ── Buttons — Apple pill style for contained, clean outlined ──────
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: {
            borderRadius: 980,     // Apple pill buttons
            textTransform: "none",
            fontWeight: 600,
            paddingInline: 20,
            transition: "transform .14s cubic-bezier(.34,1.56,.64,1), opacity .2s, background-color .2s",
            "&:hover":  { transform: "translateY(-1px)", opacity: 0.90 },
            "&:active": { transform: "translateY(0) scale(.97)" },
          },
          containedPrimary: {
            boxShadow: `0 4px 14px ${alpha(primaryMain, 0.40)}`,
            "&:hover": { boxShadow: `0 6px 20px ${alpha(primaryMain, 0.55)}` },
          },
          outlined: {
            borderRadius: 980,
            borderColor: isDark ? "rgba(255,255,255,0.20)" : "rgba(0,0,0,0.15)",
            backgroundColor: isDark ? "rgba(255,255,255,0.05)" : "rgba(255,255,255,0.70)",
            "&:hover": {
              backgroundColor: isDark ? "rgba(255,255,255,0.09)" : "rgba(255,255,255,0.90)",
              borderColor: isDark ? "rgba(255,255,255,0.30)" : "rgba(0,0,0,0.22)",
            },
          },
          text: {
            borderRadius: 980,
            "&:hover": { backgroundColor: alpha(primaryMain, isDark ? 0.12 : 0.07) },
          },
          sizeSmall: { borderRadius: 980, paddingInline: 14 },
          sizeLarge: { borderRadius: 980, paddingInline: 28 },
        },
      },

      // ── Data display ─────────────────────────────────────────────────
      MuiChip: {
        styleOverrides: {
          root: { fontWeight: 600, borderRadius: 980 },
          outlined: {
            borderColor: isDark ? "rgba(255,255,255,0.14)" : "rgba(0,0,0,0.12)",
            backgroundColor: isDark ? "rgba(255,255,255,0.05)" : "rgba(255,255,255,0.60)",
          },
        },
      },
      MuiTableContainer: {
        styleOverrides: {
          // Transparent — glass comes from the Card wrapper, not the container itself.
          root: { backgroundColor: "transparent", backgroundImage: "none", boxShadow: "none", border: "none" },
        },
      },
      MuiTableHead: {
        styleOverrides: {
          root: {
            "& .MuiTableCell-head": {
              // OPAQUE (not a translucent tint) so a stickyHeader doesn't let scrolling rows
              // bleed through it. Solid blue-tinted surface matching the card.
              backgroundColor: isDark ? "#20262f" : "#eef4fc",
              color: isDark ? "#6aadff" : "#0064c8",
              fontWeight: 700,
              fontSize: 12.5,
              letterSpacing: ".01em",
              borderBottom: `1px solid ${isDark ? "rgba(41,151,255,0.18)" : "rgba(0,113,227,0.14)"}`,
              whiteSpace: "nowrap",
            },
          },
        },
      },
      MuiTableCell: {
        styleOverrides: {
          root: { borderColor: isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.06)" },
        },
      },
      MuiTableRow: {
        styleOverrides: {
          root: {
            transition: "background-color .15s ease",
            "&:last-child td": { borderBottom: 0 },
            "&:nth-of-type(even)": {
              backgroundColor: isDark ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.015)",
            },
            "&.MuiTableRow-hover:hover": {
              backgroundColor: isDark ? "rgba(255,255,255,0.07)" : "rgba(255,255,255,0.70)",
            },
          },
        },
      },
      MuiTableSortLabel: { styleOverrides: { icon: { opacity: 0.4 } } },

      // ── Tabs ──────────────────────────────────────────────────────────
      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: "none",
            fontWeight: 500,
            minHeight: 40,
            borderRadius: 8,
            transition: "background-color .18s",
            "&.Mui-selected": {
              fontWeight: 700,
              backdropFilter: TIER2_BLUR,
              WebkitBackdropFilter: TIER2_BLUR,
              backgroundColor: isDark ? "rgba(255,255,255,0.10)" : "rgba(255,255,255,0.80)",
              boxShadow: isDark
                ? "0 0 0 0.5px rgba(255,255,255,0.12), 0 2px 8px rgba(0,0,0,0.40)"
                : "0 0 0 0.5px rgba(0,0,0,0.06), 0 2px 8px rgba(0,0,0,0.08)",
            },
            "&:hover:not(.Mui-selected)": {
              backgroundColor: isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.04)",
            },
          },
        },
      },
      MuiTabs: {
        styleOverrides: {
          root: {},
          indicator: {
            height: 2,
            borderRadius: 2,
            boxShadow: `0 0 8px 1px ${alpha(primaryMain, 0.50)}`,
          },
        },
      },

      // ── Feedback ─────────────────────────────────────────────────────
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            fontSize: 12,
            fontWeight: 500,
            borderRadius: 8,
            paddingBlock: 6,
            paddingInline: 10,
            backdropFilter: "blur(40px) saturate(180%)",
            WebkitBackdropFilter: "blur(40px) saturate(180%)",
            backgroundColor: isDark ? "rgba(28,28,30,0.92)" : "rgba(255,255,255,0.92)",
            backgroundImage: `${NOISE}`,
            border: `1px solid ${glassBorder}`,
            color: isDark ? "#f5f5f7" : "#1d1d1f",
            boxShadow: isDark
              ? "0 4px 16px rgba(0,0,0,0.60)"
              : "0 4px 16px rgba(0,0,0,0.12)",
          },
          arrow: { color: isDark ? "rgba(28,28,30,0.92)" : "rgba(255,255,255,0.92)" },
        },
      },
      MuiAlert: {
        styleOverrides: {
          root: {
            backdropFilter: "blur(40px) saturate(180%)",
            WebkitBackdropFilter: "blur(40px) saturate(180%)",
            borderRadius: 12,
            border: `1px solid ${glassBorder}`,
            boxShadow: isDark
              ? "inset 0 1px 0 rgba(255,255,255,0.06)"
              : "inset 0 1px 0 rgba(255,255,255,0.90)",
          },
        },
      },
      MuiSnackbarContent: {
        styleOverrides: {
          root: {
            backdropFilter: "blur(40px) saturate(180%)",
            WebkitBackdropFilter: "blur(40px) saturate(180%)",
            backgroundColor: isDark ? "rgba(28,28,30,0.88)" : "rgba(255,255,255,0.88)",
            backgroundImage: glassBgImage,
            border: `1px solid ${glassBorder}`,
            borderRadius: 14,
            boxShadow: isDark
              ? "0 8px 32px rgba(0,0,0,0.60)"
              : "0 8px 32px rgba(0,0,0,0.12)",
          },
        },
      },

      // ── Miscellaneous ────────────────────────────────────────────────
      MuiAccordion: {
        styleOverrides: {
          root: {
            ...glassSurface,
            borderRadius: "14px !important",
            marginBottom: 8,
            "&:before": { display: "none" },
            "&.Mui-expanded": { margin: "0 0 8px 0" },
          },
        },
      },
      MuiIconButton: {
        styleOverrides: {
          root: {
            borderRadius: 10,
            transition: "transform .14s cubic-bezier(.34,1.56,.64,1), background-color .2s",
            "&:hover":  { transform: "scale(1.10)" },
            "&:active": { transform: "scale(.94)" },
          },
        },
      },
      MuiListItemButton: {
        styleOverrides: {
          root: { borderRadius: 10, transition: "background-color .15s ease" },
        },
      },
      MuiSwitch: {
        styleOverrides: {
          root:  { padding: 8 },
          track: { borderRadius: 11, opacity: isDark ? 0.35 : 0.28 },
        },
      },
      MuiLinearProgress: {
        styleOverrides: {
          root: {
            borderRadius: 4,
            height: 6,
            backgroundColor: isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)",
          },
        },
      },
      MuiSkeleton: {
        defaultProps: { animation: "wave" },
        styleOverrides: {
          root: {
            backgroundColor: isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)",
            "&::after": {
              background: `linear-gradient(90deg,transparent,${
                isDark ? "rgba(255,255,255,0.08)" : "rgba(255,255,255,0.80)"
              },transparent)`,
            },
          },
        },
      },
      MuiDivider: {
        styleOverrides: {
          root: {
            borderColor: isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)",
          },
        },
      },
    },
  });
}


/**
 * Background for a mobile row-card NESTED inside a glass Card. Dark: a solid surface one step
 * lighter than the card (#1c1c1e) for hierarchy — NO second translucency (which, over the old
 * 7%-white card, was the source of the washed-out "haze"). Light: identical pixels to the
 * previous `alpha(background.paper, 0.48)` rendering, so light mode is unchanged.
 */
export const nestedCardBg = (t: { palette: { mode: PaletteMode } }) =>
  t.palette.mode === "dark" ? "#232326" : "rgba(255,255,255,0.48)";
