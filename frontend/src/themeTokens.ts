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
