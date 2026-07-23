// Central design-token constants. The authoritative spec is docs/DESIGN_SYSTEM.md §2
// with its machine-readable mirror docs/design-tokens.json — change those first, then
// this file (§6 governance). Grown batch-by-batch (DS01…DS09): each standardization
// batch moves the constants it touches in here; keep this module a leaf (type-only
// imports at most) so theme.ts and components can consume it freely.
import type { Palette } from "@mui/material/styles";

/** Tier-1 "content surface" blur — Cards, Accordions, resp-table row-cards, Settings
 * papers. Dark mode deliberately blurs less (anti-haze, see theme.ts rationale). */
export const TIER1_BLUR = {
  light: "blur(40px) saturate(180%)",
  dark: "blur(20px) saturate(140%)",
} as const;

/** Tier-2 "floating overlay" blur — Menu/Popover/Dialog/AppBar/Tooltip/Snackbar. */
export const TIER2_BLUR = "blur(40px) saturate(180%)";

/** Tier-2 "floating overlay" surface tint. */
export const TIER2_BG = {
  light: "rgba(255,255,255,0.88)",
  dark: "rgba(28,28,30,0.82)",
} as const;

/** Chrome blur — desktop sidebars and (since DS01) the mobile Drawer. */
export const CHROME_BLUR = "blur(48px) saturate(220%) brightness(1.03)";

/** Chrome sidebar/drawer surface tint (dark is the neutralized #1c1c1e family). */
export const CHROME_SIDEBAR_BG = {
  light: "rgba(255,255,255,.55)",
  dark: "rgba(28,28,30,.50)",
} as const;

/** Chrome sidebar/drawer inline-start hairline. */
export const CHROME_SIDEBAR_BORDER = {
  light: "rgba(255,255,255,.75)",
  dark: "rgba(255,255,255,.10)",
} as const;

/** Desktop sidebar width — admin and portal alike (§5-C10). */
export const SIDEBAR_WIDTH = 256;

/** Sidebar top ambient glow — Apple blue, one per mode (§5-C1). */
export const SIDEBAR_AMBIENT = {
  light: "radial-gradient(ellipse 120% 80% at 50% 0%, rgba(0,113,227,.18) 0%, transparent 70%)",
  dark: "radial-gradient(ellipse 120% 80% at 50% 0%, rgba(0,113,227,.26) 0%, transparent 70%)",
} as const;

/** Specular gloss for the selected nav item surface. */
export const GLOSS_NAV_SELECTED =
  "linear-gradient(175deg,rgba(255,255,255,.12) 0%,rgba(255,255,255,0) 60%)";

/** Specular gloss for the 31px nav icon chips. */
export const GLOSS_NAV_CHIP =
  "linear-gradient(145deg,rgba(255,255,255,.24) 0%,rgba(255,255,255,0) 60%)";

/** Invoice status → palette color, for chart fills and legends (§5-C8). */
export const invoiceStatusColor = (palette: Palette, status: string): string =>
  (
    {
      paid: palette.success.main,
      sent: palette.info.main,
      overdue: palette.warning.main,
      enforced: palette.error.main,
    } as Record<string, string>
  )[status] ?? palette.text.secondary;

/**
 * StatusPill colors (§5-C8). The pill renders 12px/750 text in the color itself, so
 * light mode uses the MUI-computed `dark` variants for contrast on light glass;
 * dark mode uses the canonical mains.
 */
export const statusPillColors = (palette: Palette) => ({
  active: palette.mode === "light" ? palette.success.dark : palette.success.main,
  muted: palette.text.secondary,
  enforced: palette.error.main,
  frozen: palette.mode === "light" ? palette.warning.dark : palette.warning.main,
});

/** Desktop table scroll bound under the page header (§5-C20, D14-confirmed). */
export const TABLE_SCROLL_BOUND = "calc(100vh - 300px)";

/** Standard entrance ease — framer tuple + its CSS bezier spelling. */
export const EASE_ENTRANCE = [0.22, 1, 0.36, 1] as const;
export const ENTRANCE_BEZIER = "cubic-bezier(.22,1,.36,1)";

/** Overshoot spring bezier for presses/hovers. */
export const SPRING_BEZIER = "cubic-bezier(.34,1.56,.64,1)";

/** Chart text family — the ECharts canvas cannot inherit CSS fonts. */
export const CHART_FONT = "Vazirmatn, sans-serif";

/** Specular gloss for the 44px StatCard icon tiles. */
export const GLOSS_STATCARD_TILE =
  "linear-gradient(145deg,rgba(255,255,255,.28) 0%,rgba(255,255,255,0) 60%)";
