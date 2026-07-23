import { describe, expect, it } from "vitest";
import type { PaletteMode } from "@mui/material";
import { makeTheme } from "../theme";
import { chartTooltip } from "../components/chartTooltip";
import {
  CHROME_BLUR,
  CHROME_SIDEBAR_BG,
  CHROME_SIDEBAR_BORDER,
  GLOSS_NAV_CHIP,
  GLOSS_NAV_SELECTED,
  SIDEBAR_AMBIENT,
  SIDEBAR_WIDTH,
  TABLE_SCROLL_BOUND,
  TIER1_BLUR,
  TIER2_BG,
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

  it("matches tier-1 blur and tier-2 bg (DS03)", () => {
    expect(TIER1_BLUR).toEqual({ light: "blur(40px) saturate(180%)", dark: "blur(20px) saturate(140%)" });
    expect(TIER2_BG).toEqual({ light: "rgba(255,255,255,0.88)", dark: "rgba(28,28,30,0.82)" });
  });

  it("chrome parity tokens are Apple blue, never violet (C1/C10, DS05)", () => {
    expect(SIDEBAR_WIDTH).toBe(256);
    expect(SIDEBAR_AMBIENT.dark).toBe(
      "radial-gradient(ellipse 120% 80% at 50% 0%, rgba(0,113,227,.26) 0%, transparent 70%)",
    );
    expect(SIDEBAR_AMBIENT.light).toBe(
      "radial-gradient(ellipse 120% 80% at 50% 0%, rgba(0,113,227,.18) 0%, transparent 70%)",
    );
    expect(SIDEBAR_AMBIENT.dark).not.toContain("139,92,246");
    expect(GLOSS_NAV_SELECTED).toBe(
      "linear-gradient(175deg,rgba(255,255,255,.12) 0%,rgba(255,255,255,0) 60%)",
    );
    expect(GLOSS_NAV_CHIP).toBe(
      "linear-gradient(145deg,rgba(255,255,255,.24) 0%,rgba(255,255,255,0) 60%)",
    );
  });

  it("table scroll bound is the single D14-confirmed token (C20/DS06)", () => {
    expect(TABLE_SCROLL_BOUND).toBe("calc(100vh - 300px)");
  });
});

describe("theme consumes the tier tokens (DS03)", () => {
  for (const mode of MODES) {
    it(`tier-1 Card blur and tier-2 Menu surface in ${mode} mode`, () => {
      const card = overrides(mode, "MuiCard", "root");
      expect(card.backdropFilter).toBe(TIER1_BLUR[mode]);
      const menu = overrides(mode, "MuiMenu", "paper");
      expect(menu.backgroundColor).toBe(TIER2_BG[mode]);
      expect(menu.backdropFilter).toBe(TIER2_BLUR);
    });
  }
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

describe("chart tooltips use the glass surface + theme text (C2/DS04, D4)", () => {
  it("light mode", () => {
    const t = makeTheme("light");
    const tip = chartTooltip(t);
    expect(tip.backgroundColor).toBe("rgba(255,255,255,0.88)");
    expect(tip.borderColor).toBe("rgba(0,0,0,0.05)");
    expect(tip.textStyle.color).toBe(t.palette.text.primary);
    expect(tip.textStyle.fontFamily).toBe("Vazirmatn, sans-serif");
  });
  it("dark mode uses the neutral #1c1c1e-family surface, not legacy navy", () => {
    const t = makeTheme("dark");
    const tip = chartTooltip(t);
    expect(tip.backgroundColor).toBe("rgba(28,28,30,0.92)");
    expect(tip.borderColor).toBe("rgba(255,255,255,0.14)");
    expect(tip.textStyle.color).toBe(t.palette.text.primary);
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
