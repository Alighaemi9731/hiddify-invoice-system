#!/usr/bin/env node
// Design-token drift guardrail (DS09, DESIGN_SYSTEM.md §6). Scans frontend/src for
// styling values and fails if any value is not canonical per docs/design-tokens.json.
// Extending the allowlist requires a doc-first token addition (§6 governance) — the
// color allowlist is HARVESTED from the tokens JSON at lint time, so adding a token
// there (after updating DESIGN_SYSTEM.md) is the only way to introduce a new color.
// Run: `npm run lint:design`. Wired into the CI frontend job.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(HERE, "..");
const SRC = join(FRONTEND, "src");
const TOKENS_PATH = join(FRONTEND, "..", "docs", "design-tokens.json");

// ── Allowlists ──────────────────────────────────────────────────────────────
// Colors: every hex/rgba that appears anywhere in the canonical tokens JSON.
const tokensText = readFileSync(TOKENS_PATH, "utf8").toLowerCase();
const allowColor = (v) => tokensText.includes(v.toLowerCase());

// The canonical blur trio (blurTiers.canonical) — the only recipes allowed.
const BLUR_OK = new Set([
  "40/180",
  "20/140",
  "48/220/1.03",
]);

// Radii: sx multipliers ×14 (0–5 steps in use) + literal px steps + strings (§2.8).
const RADIUS_OK = new Set([
  "0", "0.5", "1", "1.5", "2", "2.5", "3", "4", "5", // sx multipliers in use
  "6", "7", "8", "10", "11", "12", "14", "18", "20", "980", // literal px steps
  "14px", "18px", "24px", "50px", "50%", "14px !important", "inherit",
]);

// Font sizes (§2.7 vocabulary) and weights (500–850 ramp).
const FONTSIZE_OK = new Set([
  "9", "11", "11.5", "12", "12.5", "13", "13.5", "14", "14.5", "15", "15.5", "16",
  "17", "18", "19", "23", "25", "27", "36", "48", "1.35rem", "1.65rem", "1.35", "1.65",
]);
const FONTWEIGHT_OK = new Set(["500", "600", "650", "700", "750", "800", "850"]);

// z-index layers (§2.12) beyond MUI defaults.
const ZINDEX_OK = new Set(["-1", "0", "1", "2", "3"]);

// File-scoped exceptions:
// - Login.tsx is the sanctioned C4 hero-screen exception (its whites + field radii).
// - themeTokens.ts DEFINES the scroll-bound literal.
// - Test files assert legacy values in negative assertions.
const skipFile = (rel) => rel.includes(".test.") || rel.includes("/test/");
const loginException = (rel) => rel.endsWith("pages/Login.tsx");
const tokensModule = (rel) => rel.endsWith("themeTokens.ts");
// The one file allowed to name `type="number"` — its doc comment explains why nobody else may.
const numberFieldModule = (rel) => rel.endsWith("components/NumberField.tsx");

// ── Extraction (ported from the Phase-2 audit sweep) ────────────────────────
const BLUR_RE = /(?:blur\((\d+)px\)\s*saturate\((\d+)%\)(?:\s*brightness\(([\d.]+)\))?|saturate\((\d+)%\)\s*blur\((\d+)px\))/g;

export function lintText(rel, text) {
  const findings = [];
  const flag = (line, category, value) => findings.push({ file: rel, line, category, value });
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const stripped = raw.trimStart();
    if (stripped.startsWith("//") || stripped.startsWith("*") || stripped.startsWith("/*")) continue;
    const line = raw.includes("//") ? raw.split("//")[0] : raw;
    const n = i + 1;

    for (const m of line.matchAll(/#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b/g)) {
      if (!allowColor(m[0])) flag(n, "hex", m[0]);
    }
    for (const m of line.matchAll(/rgba?\([0-9.,\s]+\)/g)) {
      const v = m[0].replace(/\s+/g, "");
      if (loginException(rel)) continue;
      if (!allowColor(v)) flag(n, "rgba", v);
    }
    for (const m of line.matchAll(BLUR_RE)) {
      const v = m[1]
        ? `${m[1]}/${m[2]}${m[3] ? `/${m[3]}` : ""}`
        : `${m[5]}/${m[4]}`;
      if (!BLUR_OK.has(v)) flag(n, "blur", v);
    }
    if (line.includes("borderRadius") && !loginException(rel)) {
      for (const m of line.matchAll(/borderRadius:\s*(?:"([^"]+)"|'([^']+)'|([\d.]+)|\[([\d,\s]+)\])/g)) {
        const vals = m[4] ? m[4].split(",").map((x) => x.trim()) : [m[1] ?? m[2] ?? m[3]];
        for (const v of vals) if (v && !RADIUS_OK.has(v)) flag(n, "radius", v);
      }
    }
    for (const m of line.matchAll(/fontSize:\s*(?:"([\d.]+rem)"|([\d.]+))/g)) {
      const v = m[1] ?? m[2];
      if (!FONTSIZE_OK.has(v)) flag(n, "fontSize", v);
    }
    for (const m of line.matchAll(/fontSize:\s*\{([^}]*)\}/g)) {
      for (const v of m[1].match(/[\d.]+(?:rem)?/g) ?? []) {
        if (!FONTSIZE_OK.has(v)) flag(n, "fontSize", v);
      }
    }
    for (const m of line.matchAll(/fontWeight:\s*(\d+)/g)) {
      if (!FONTWEIGHT_OK.has(m[1])) flag(n, "fontWeight", m[1]);
    }
    for (const m of line.matchAll(/zIndex:\s*(-?\d+)/g)) {
      if (!ZINDEX_OK.has(m[1])) flag(n, "zIndex", m[1]);
    }
    // §4.3a hard rule: a raw number input mis-places the caret in this RTL app and cannot be
    // cleared. Numeric inputs go through `NumberField`.
    if (!numberFieldModule(rel) && /type=\{?["']number["']\}?/.test(line)) {
      flag(n, "numberInput", 'type="number" — use components/NumberField');
    }
    for (const m of line.matchAll(/calc\(100vh - (\d+)px\)/g)) {
      if (!(tokensModule(rel) && m[1] === "300")) flag(n, "scrollBound", `calc(100vh - ${m[1]}px)`);
    }
  }
  return findings;
}

export function lintTree() {
  const findings = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) { walk(p); continue; }
      if (!/\.(ts|tsx)$/.test(name)) continue;
      const rel = relative(FRONTEND, p).replaceAll("\\", "/");
      if (skipFile(rel)) continue;
      findings.push(...lintText(rel, readFileSync(p, "utf8")));
    }
  };
  walk(SRC);
  return findings;
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  const findings = lintTree();
  if (findings.length) {
    console.error(`design-lint: ${findings.length} non-canonical value(s):`);
    for (const f of findings) console.error(`  ${f.file}:${f.line}  [${f.category}] ${f.value}`);
    console.error("Add tokens doc-first (DESIGN_SYSTEM.md §6 → design-tokens.json) or fix the value.");
    process.exit(1);
  }
  console.log("design-lint: all styling values are canonical.");
}
