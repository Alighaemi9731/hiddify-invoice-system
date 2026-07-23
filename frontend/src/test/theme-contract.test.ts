import { describe, expect, it } from "vitest";
import type { PaletteMode } from "@mui/material";
import { makeTheme } from "../theme";
import {
  CHROME_BLUR,
  CHROME_SIDEBAR_BG,
  CHROME_SIDEBAR_BORDER,
  TIER2_BLUR,
  invoiceStatusColor,
  statusPillColors,
} from "../themeTokens";

// Contract tests for DESIGN_SYSTEM.md §2 / design-tokens.json — the theme must equal
// the documented tokens (DS01/C16/C5). Values are asserted literally on purpose: a
// token change must be a conscious doc-first edit, not a drive-by.

const MODES: PaletteMode[] = ["light", "dark"];

const overrides = (mode: PaletteMode, component: string, slot: string) => {
  const c = makeTheme(mode).components as Record<string, any>;
  return c[component]?.styleOverrides?.[slot] as Record<string, any>;
};

describe("themeTokens spec pinning", () => {
  it("matches design-tokens.json blurTiers.canonical + glass.chrome", () => {
    expect(TIER2_BLUR).toBe("blur(40px) saturate(180%)");
    expect(CHROME_BLUR).toBe("blur(48px) saturate(220%) brightness(1.03)");
    expect(CHROME_SIDEBAR_BG).toEqual({ light: "rgba(255,255,255,.55)", dark: "rgba(28,28,30,.50)" });
    expect(CHROME_SIDEBAR_BORDER).toEqual({ light: "rgba(255,255,255,.75)", dark: "rgba(255,255,255,.10)" });
  });
});

describe("MuiDrawer paper = chrome glass (C16)", () => {
  for (const mode of MODES) {
    it(`uses the chrome recipe in ${mode} mode`, () => {
      const paper = overrides(mode, "MuiDrawer", "paper");
      expect(paper.backdropFilter).toBe(CHROME_BLUR);
      expect(paper.WebkitBackdropFilter).toBe(CHROME_BLUR);
      expect(paper.backgroundColor).toBe(CHROME_SIDEBAR_BG[mode]);
      expect(paper.borderInlineStart).toBe(`1px solid ${CHROME_SIDEBAR_BORDER[mode]}`);
      // The noise/gloss layer must survive the recipe change.
      expect(paper.backgroundImage).toBeTruthy();
    });
  }
});

describe("selected Tab = tier-2 blur (C5 theme site)", () => {
  for (const mode of MODES) {
    it(`uses TIER2_BLUR in ${mode} mode`, () => {
      const root = overrides(mode, "MuiTab", "root");
      const selected = root["&.Mui-selected"] as Record<string, any>;
      expect(selected.backdropFilter).toBe(TIER2_BLUR);
      expect(selected.WebkitBackdropFilter).toBe(TIER2_BLUR);
    });
  }
});

describe("semantic status colors come from the palette (C8/DS02)", () => {
  for (const mode of MODES) {
    it(`invoiceStatusColor maps every status to the ${mode} palette`, () => {
      const p = makeTheme(mode).palette;
      expect(invoiceStatusColor(p, "paid")).toBe(p.success.main);
      expect(invoiceStatusColor(p, "sent")).toBe(p.info.main);
      expect(invoiceStatusColor(p, "overdue")).toBe(p.warning.main);
      expect(invoiceStatusColor(p, "enforced")).toBe(p.error.main);
      expect(invoiceStatusColor(p, "anything-else")).toBe(p.text.secondary);
    });
  }

  it("statusPillColors uses the darker computed variants for 12px text on light glass", () => {
    const p = makeTheme("light").palette;
    const colors = statusPillColors(p);
    expect(colors.active).toBe(p.success.dark);
    expect(colors.frozen).toBe(p.warning.dark);
    expect(colors.muted).toBe(p.text.secondary);
    expect(colors.enforced).toBe(p.error.main);
    // MUI must actually have computed the variants from the canonical mains.
    expect(p.success.dark).toBeTruthy();
    expect(p.success.dark).not.toBe(p.success.main);
  });

  it("statusPillColors uses the canonical mains in dark mode", () => {
    const p = makeTheme("dark").palette;
    const colors = statusPillColors(p);
    expect(colors.active).toBe(p.success.main);
    expect(colors.frozen).toBe(p.warning.main);
    expect(colors.muted).toBe(p.text.secondary);
    expect(colors.enforced).toBe(p.error.main);
  });
});

describe("tier-2 overlays are untouched by the DS01 refactor", () => {
  for (const mode of MODES) {
    it(`keeps Snackbar/Menu surfaces byte-identical in ${mode} mode`, () => {
      const snackbar = overrides(mode, "MuiSnackbarContent", "root");
      expect(snackbar.backgroundColor).toBe(
        mode === "dark" ? "rgba(28,28,30,0.88)" : "rgba(255,255,255,0.88)",
      );
      expect(snackbar.backdropFilter).toBe("blur(40px) saturate(180%)");
      const menu = overrides(mode, "MuiMenu", "paper");
      expect(menu.backdropFilter).toBe("blur(40px) saturate(180%)");
    });
  }
});
