# Design System — سامانه مدیریت و فاکتور نمایندگان

**Version 2.0 — "standardized", 2026-07-23.** v1.0 was extracted verbatim from the
code at `v1.100.4`; the DS01–DS09 standardization program then made the code conform
to it (§5 records every resolution). Since DS09, conformance is machine-enforced:
`frontend/scripts/design-lint.mjs` (`npm run lint:design`, run in CI) fails on any
styling value not present in `docs/design-tokens.json`.
This document is the **single source of truth for every UI decision** in `frontend/`.
It describes what the code does today — nothing here is invented. Every value carries a
`file:line` citation (paths relative to `frontend/` unless prefixed). Where the code
disagrees with itself, all variants are recorded in **§5 Conflict log** and exactly one
is designated canonical. A machine-readable mirror of the canonical tokens lives in
[`docs/design-tokens.json`](design-tokens.json).

Scope: the React SPA (admin panel + reseller portal + storefront admin), its static
assets, and chart styling. Out of scope: Telegram bot message formatting, backend, PDF
rendering, and Persian copywriting (except where copy is effectively a design rule).

How styling is organized in this codebase:

- There are **no CSS files**. All styling is CSS-in-JS: `src/theme.ts` (MUI theme +
  `CssBaseline` global styles) plus per-component `sx` props. The only other style
  island is the inline `<style>` block in `index.html:32-42` (font-face + root sizing).
- `src/theme.ts` `makeTheme(mode)` is the intended source of truth and wins over ad-hoc
  styling by rule (see §5 canonical-selection rules).
- RTL is produced by emotion + `stylis-plugin-rtl` (`src/rtlCache.ts:5-8`) with
  `direction: "rtl"` (`src/theme.ts:104`) — physical CSS is auto-flipped; code prefers
  logical properties (`paddingInline`, `insetInlineStart`, `borderInlineStart`, …).

**Two unit semantics you must know before enforcing anything** (MUI defaults, not
overridden in `makeTheme`, `src/theme.ts:103`):

1. `sx={{ borderRadius: N }}` with a **number** multiplies `theme.shape.borderRadius`,
   which this app sets to **14** (`src/theme.ts:130`). So `borderRadius: 2` = 28px,
   `2.5` = 35px, `3` = 42px. **String** values (`"14px"`, `"50px"`) and numbers inside
   `styleOverrides` are literal pixels.
2. `sx` spacing props (`p`, `m`, `gap`, `spacing`) multiply the default **8px** unit:
   `p: 1.5` = 12px, `mx: 1.25` = 10px.

---

## §1 Design language & principles

### 1.1 The current language: Apple-glass ("Liquid Glass", Apple edition)

The theme was rebuilt around **apple.com's computed styles** (stated in code comments,
`src/theme.ts:15-21, 269`). The earlier indigo-violet "fintech" glass language (M35/M36)
survives only as remnants, which are catalogued in §5. The system today:

- **Accent identity — Apple system blue.** `#0071e3` light / `#2997ff` dark
  (`src/theme.ts:16`). It is simultaneously `primary` and `info` (`src/theme.ts:107,115`).
  The brand mark is a receipt glyph on a `#5ab5ff → #0071e3` gradient tile
  (`src/components/Layout.tsx:144`, favicon variant `#5ab5ff → #0064d2`,
  `public/favicon.svg`).
- **Pure, near-monochrome canvases.** Dark mode sits on true black `#000000`; light on
  Apple grey `#f5f5f7` (`src/theme.ts:109-111,150`). Instead of the old multi-color
  ambient blobs, the body carries **one** faint top-center brand glow
  (`src/theme.ts:89-93,152-159`): dark
  `radial-gradient(100% 40% at 50% 0%, rgba(41,151,255,0.06), transparent 60%)`, light
  the same shape at `rgba(0,113,227,0.025)`, painted on `body::before` at `z-index: -1`.
- **Two-tier glass.** Tier‑1 "content" surfaces (cards, table wrappers) are
  **near-opaque in dark** — `rgba(28,28,30,0.90)` — because stacked translucency over
  blur produced an unreadable haze (rationale comment `src/theme.ts:23-26`); light
  tier‑1 is `rgba(255,255,255,0.78)`. Tier‑2 "floating" overlays (menus, popovers,
  dialogs, app chrome) stay more translucent (`rgba(28,28,30,0.82)` dark /
  `rgba(255,255,255,0.88)` light) with the full `blur(40px) saturate(180%)`
  (`src/theme.ts:27-33`).
- **The glass recipe** (per surface): backdrop blur+saturate, a translucent background
  color, a **noise micro-texture** SVG at 3% opacity (`src/theme.ts:4-10`), in light
  mode a restrained top-light gloss gradient
  (`linear-gradient(175deg, rgba(255,255,255,0.70) 0%, rgba(255,255,255,0) 50%)`,
  `src/theme.ts:61-67`; dark mode deliberately noise-only), a 1px hairline border, and
  a composite shadow of three parts: a **0.5px ring** (`0 0 0 0.5px …`), a soft depth
  drop, and a **specular top-rim inset** (`inset 0 1px 0 rgba(255,255,255,…)`)
  (`src/theme.ts:35-58`). "Border defines the element, not a heavy drop"
  (`src/theme.ts:35`).
- **Pill geometry.** Buttons and chips use radius 980 ("Apple pill",
  `src/theme.ts:341,374`); single-line inputs use radius `50px` (`src/theme.ts:315-316`);
  cards 18, dialogs 20, base radius 14 (`src/theme.ts:130,223,248`).
- **Motion is soft and spring-y.** One standard entrance ease
  `cubic-bezier(.22,1,.36,1)` and one overshoot spring `cubic-bezier(.34,1.56,.64,1)`
  for presses/hovers; staggered row entrances; everything collapses to ~0 under
  `prefers-reduced-motion` (§2.13).
- **Dark & light are peers.** Every surface/token is defined per mode in the same
  expression; first visit follows the OS, then the header toggle persists to
  `localStorage("color_mode")` (`src/main.tsx:15-20,32-37`), and the browser chrome
  `<meta name="theme-color">` is kept in sync (`#000000`/`#f5f5f7`,
  `src/main.tsx:24-28`, `index.html:18-20`).
- **RTL-first, Persian-first.** `<html lang="fa" dir="rtl">` (`index.html:2`), Vazirmatn
  variable font self-hosted (`index.html:21-42`), Persian digits for display numbers
  (`src/format.ts`), with a hard rule for when a field's *input content* is LTR (§4.2).
- **Accessibility baselines built in:** global `:focus-visible` ring
  (`src/theme.ts:210-213`), `::selection` tint (`src/theme.ts:209`), reduced-motion
  kill-switch (`src/theme.ts:206-208`), canvas charts get `role="img"` + Persian
  `aria-label` (`src/components/EChart.tsx:43-50`), dark text.secondary lifted to
  ≈6.6:1 contrast on `#1c1c1e` (`src/theme.ts:118`).

### 1.2 Surface hierarchy (what sits on what)

1. `body` — flat mode canvas + fixed ambient glow (`src/theme.ts:146-159`).
2. **Chrome** — sidebar / AppBar / BottomNav: strongest blur, most translucent
   (§2.10 tier "chrome"; `src/components/Layout.tsx:245-253`, `src/theme.ts:270-283`,
   `src/components/BottomNav.tsx:29-42`).
3. **Tier‑1 content glass** — `MuiCard` (and Accordion) via the `glassSurface` mixin
   (`src/theme.ts:70-77,219-227,511-522`).
4. **Nested row-cards** *inside* a Card — a solid step-lighter surface in dark
   (`#232326`), `rgba(255,255,255,0.48)` in light — explicitly **no second
   translucency** (`nestedCardBg`, `src/theme.ts:578-585`).
5. **Tier‑2 floating glass** — Menu/Popover/Dialog/Tooltip/Snackbar via `floatSurface`
   (`src/theme.ts:80-87,234-257,461-509`), over a mode-aware `MuiBackdrop`
   (`src/theme.ts:253-257`).

---

## §2 Design tokens

Everything in this section exists in code today. "sx-N" marks a numeric `sx` radius
(multiplier semantics, ×14px). Canonical values are what `design-tokens.json` mirrors;
variant values that lost a conflict appear only in §5.

### 2.1 Color — accent & semantic palette (`src/theme.ts:105-120`)

| Token | Light | Dark | Source |
|---|---|---|---|
| `primary.main` | `#0071e3` | `#2997ff` | `src/theme.ts:16,107` |
| `secondary.main` (Apple orange) | `#ff9500` | `#ff9f0a` | `src/theme.ts:108` |
| `success.main` (Apple green) | `#28cd41` | `#30d158` | `src/theme.ts:112` |
| `error.main` (Apple red) | `#ff3b30` | `#ff453a` | `src/theme.ts:113` |
| `warning.main` (Apple yellow/orange) | `#ff9500` | `#ffd60a` | `src/theme.ts:114` |
| `info.main` (= primary) | `#0071e3` | `#2997ff` | `src/theme.ts:115` |
| `divider` | `rgba(0,0,0,0.08)` | `rgba(255,255,255,0.10)` | `src/theme.ts:116` |
| `background.default` | `#f5f5f7` | `#000000` | `src/theme.ts:109-111` |
| `background.paper` (= tier‑1 glass bg) | `rgba(255,255,255,0.78)` | `rgba(28,28,30,0.90)` | `src/theme.ts:27,110-111` |
| `text.primary` | `#1d1d1f` | `#f5f5f7` | `src/theme.ts:117-119` |
| `text.secondary` | `#6e6e73` | `#a1a1a6` | `src/theme.ts:117-119` |

Notes: `secondary` duplicates light `warning` (`#ff9500`), and `info` duplicates
`primary` — both are code facts, listed as gaps (§5.3-G7). Text has only two authored
tiers; `text.disabled` is MUI-derived (used e.g. `src/components/TelegramLink.tsx:22`).

### 2.2 Color — glass surface tokens (both modes)

| Token | Light | Dark | Source |
|---|---|---|---|
| Tier‑1 `glassBg` | `rgba(255,255,255,0.78)` | `rgba(28,28,30,0.90)` | `src/theme.ts:27` |
| Tier‑1 `glassBorder` | `rgba(0,0,0,0.05)` | `rgba(255,255,255,0.12)` | `src/theme.ts:28` |
| Tier‑1 `glassBlur` | `blur(40px) saturate(180%)` | `blur(20px) saturate(140%)` | `src/theme.ts:29` |
| Tier‑2 `floatBg` | `rgba(255,255,255,0.88)` | `rgba(28,28,30,0.82)` | `src/theme.ts:32` |
| Tier‑2 `floatBlur` | `blur(40px) saturate(180%)` (both modes) | | `src/theme.ts:33` |
| Noise texture | inline SVG `fractalNoise`, opacity 0.03, baseFrequency 0.85 | | `src/theme.ts:4-10` |
| _(reference only)_ apple.com values quoted in comments — pill bg `rgba(255,255,255,0.1)`, localnav dark `rgba(0,0,0,0.6)`, pill border `rgba(217,207,207,0.25)`, nav light `rgba(255,255,255,0.8)` | not applied styles — documentation comments | | `src/theme.ts:18-21,269` |
| Top-light gloss `glassInner` | `linear-gradient(175deg, rgba(255,255,255,0.70) 0%, rgba(255,255,255,0) 50%)` | (omitted in dark — noise only) | `src/theme.ts:61-67` |
| `nestedCardBg` (row-card inside a Card) | `rgba(255,255,255,0.48)` | `#232326` (solid) | `src/theme.ts:578-585` |
| `resp-table` mobile row-card bg | `rgba(255,255,255,0.72)` | `rgba(36,36,38,0.94)` | `src/theme.ts:187` |
| Drawer paper (= chrome recipe since `v1.100.5`/C16) | `rgba(255,255,255,.55)` | `rgba(28,28,30,.50)`, `blur(48px) saturate(220%) brightness(1.03)`, border `glass.chrome.sidebarBorder` | `src/theme.ts` MuiDrawer block, via `src/themeTokens.ts` |
| AppBar | `rgba(255,255,255,0.80)` | `rgba(0,0,0,0.60)`, `blur(40px) saturate(180%)`, noise, ring `0 0 0 0.5px` | `src/theme.ts:270-283` |
| BottomNav | `rgba(255,255,255,0.86)` | `rgba(0,0,0,0.72)`, `blur(40px) saturate(180%)` | `src/components/BottomNav.tsx:38-41` |
| Desktop sidebar (admin & portal — chrome tokens since DS04) | `rgba(255,255,255,.55)` | `rgba(28,28,30,.50)`, `CHROME_BLUR` | `src/themeTokens.ts` via `sidebarGlassSx` in `src/components/Layout.tsx` + `src/portal/PortalLayout.tsx` |
| Tooltip | `rgba(255,255,255,0.92)` | `rgba(28,28,30,0.92)`, `blur(40px) saturate(180%)`, noise | `src/theme.ts:461-481` |
| Snackbar | `rgba(255,255,255,0.88)` | `rgba(28,28,30,0.88)`, `blur(40px) saturate(180%)` | `src/theme.ts:495-509` |
| Modal backdrop | `rgba(0,0,0,0.20)` | `rgba(0,0,0,0.50)` | `src/theme.ts:253-257` |
| Settings section/sidebar Paper glass (tier‑1 since DS03) | `TIER1_BLUR` mode split on `Paper variant="outlined"` | | `src/pages/Settings.tsx` renderSection + side-nav Paper |
| Settings sticky header (tier‑2 since DS03) | `TIER2_BG` + `TIER2_BLUR` (light `rgba(255,255,255,0.88)`) | dark `rgba(28,28,30,0.82)` | `src/pages/Settings.tsx` sticky header (its light shadow stays navy → §5-C2/DS04) |
| Login hero card | light `rgba(255,255,255,0.62)` / dark `rgba(28,28,30,0.55)` (neutralized DS04), `CHROME_BLUR` (since DS03), radius `24px` | | `src/pages/Login.tsx:150-185` |

### 2.3 Color — component-state tints (the alpha-tint vocabulary)

Recurring `rgba(255,255,255,…)` / `rgba(0,0,0,…)` interaction tints:

| Role | Light | Dark | Source |
|---|---|---|---|
| Input resting bg | `rgba(255,255,255,0.80)` | `rgba(255,255,255,0.06)` | `src/theme.ts:318` |
| Input hover bg | `rgba(255,255,255,0.95)` | `rgba(255,255,255,0.09)` | `src/theme.ts:319-321` |
| Input focus bg | `rgba(255,255,255,1.0)` | `rgba(255,255,255,0.08)` | `src/theme.ts:325-327` |
| Input outline | `rgba(0,0,0,0.12)` | `rgba(255,255,255,0.10)` | `src/theme.ts:330-332` |
| Input outline hover | `rgba(0,0,0,0.18)` | `rgba(255,255,255,0.22)` | `src/theme.ts:322-324` |
| Outlined button bg / border | `rgba(255,255,255,0.70)` / `rgba(0,0,0,0.15)` | `rgba(255,255,255,0.05)` / `rgba(255,255,255,0.20)` | `src/theme.ts:353-360` |
| Outlined chip bg / border | `rgba(255,255,255,0.60)` / `rgba(0,0,0,0.12)` | `rgba(255,255,255,0.05)` / `rgba(255,255,255,0.14)` | `src/theme.ts:375-379` |
| Table zebra (even rows) | `rgba(0,0,0,0.015)` | `rgba(255,255,255,0.03)` | `src/theme.ts:414-416` |
| Table row hover | `rgba(255,255,255,0.70)` | `rgba(255,255,255,0.07)` | `src/theme.ts:417-419` |
| Table cell border | `rgba(0,0,0,0.06)` | `rgba(255,255,255,0.05)` | `src/theme.ts:404-408` |
| Tab selected bg | `rgba(255,255,255,0.80)` + `blur(20px) saturate(180%)` | `rgba(255,255,255,0.10)` | `src/theme.ts:434-441` |
| Tab hover bg | `rgba(0,0,0,0.04)` | `rgba(255,255,255,0.05)` | `src/theme.ts:443-445` |
| Nav item selected bg / selected-hover | `rgba(255,255,255,.60)` / `rgba(255,255,255,.76)` | `rgba(255,255,255,.07)` / `rgba(255,255,255,.10)` | `src/components/Layout.tsx:79-88` |
| Nav item hover bg | `rgba(255,255,255,.38)` | `rgba(255,255,255,.05)` | `src/components/Layout.tsx:101-103` |
| SegmentedTabs container bg / tab hover | `rgba(255,255,255,0.55)` / `rgba(255,255,255,.55)` | `rgba(255,255,255,0.05)` / `rgba(255,255,255,.06)` | `src/components/SegmentedTabs.tsx:33,76-78` |
| Select dropdown icon | `rgba(0,0,0,0.28)` | `rgba(255,255,255,0.35)` | `src/theme.ts:295-298` |
| Divider (MuiDivider) | `rgba(0,0,0,0.08)` | `rgba(255,255,255,0.08)` | `src/theme.ts:566-572` |
| Skeleton base / shimmer | `rgba(0,0,0,0.06)` / `rgba(255,255,255,0.80)` | `rgba(255,255,255,0.06)` / `rgba(255,255,255,0.08)` | `src/theme.ts:553-565` |
| LinearProgress track | `rgba(0,0,0,0.06)` | `rgba(255,255,255,0.08)` | `src/theme.ts:544-552` |
| Scrollbar thumb / hover | `rgba(0,0,0,.12)` / `rgba(0,0,0,.20)` | `rgba(255,255,255,.14)` / `rgba(255,255,255,.22)` | `src/theme.ts:136-144` |
| `::selection` | `alpha(primary, 0.28)` | same | `src/theme.ts:209` |
| Focus ring | `2px solid alpha(primary, 0.80)`, offset 2, radius 4 | same | `src/theme.ts:210-213` |
| Neutral inset box (copyables, key-values) | `bgcolor: "action.hover"` | same | e.g. `src/portal/PayDialog.tsx:34`, `src/portal/pages/Panels.tsx:69,107`, `src/pages/Payments.tsx:371` |

Alpha-of-accent recipes used with `alpha()` from `@mui/material/styles`:
`0.05` (LiveRate bg, `src/components/LiveRate.tsx:43`), `0.07/0.12` (text-button hover
light/dark, `src/theme.ts:364`), `0.09` (tree toggle bg,
`src/pages/resellers/ResellerIdentity.tsx:124`), `0.10/0.18` (SegmentedTabs selected
light/dark, `src/components/SegmentedTabs.tsx:65-67`), `0.22` (input focus ring,
`src/theme.ts:327`), `0.28` (selection), `0.40/0.55` (contained button glow rest/hover,
`src/theme.ts:350-351`), `0.42` (nav accent-bar glow, `src/components/Layout.tsx:98`),
`0.50` (tabs indicator glow, `src/theme.ts:455`), `0.80` (focus ring).

### 2.4 Color — categorical nav/icon palette

One accent hex per destination, shared by the sidebar icon chips (admin + portal):

| Destination | Hex | Source |
|---|---|---|
| Dashboard | `#0071e3` | `src/components/Layout.tsx:40`, `src/portal/PortalLayout.tsx:33` |
| Panels | `#0ea5e9` | `src/components/Layout.tsx:41`, `src/portal/PortalLayout.tsx:37` |
| Resellers / Subs | `#22c55e` | `src/components/Layout.tsx:42`, `src/portal/PortalLayout.tsx:36` |
| Invoices | `#f59e0b` | `src/components/Layout.tsx:43`, `src/portal/PortalLayout.tsx:34` |
| Payments | `#10b981` | `src/components/Layout.tsx:44`, `src/portal/PortalLayout.tsx:35` |
| Debts | `#f43f5e` | `src/components/Layout.tsx:45` |
| Sales | `#30d158` | `src/components/Layout.tsx:46` |
| Financial history | `#14b8a6` | `src/components/Layout.tsx:47` |
| Broadcast / Storefront | `#ec4899` | `src/components/Layout.tsx:48`, `src/portal/PortalLayout.tsx:38` |
| Logs | `#0891b2` | `src/components/Layout.tsx:49` |
| Account | `#3b82f6` | `src/components/Layout.tsx:50` |
| Tools | `#8b5cf6` | `src/components/Layout.tsx:51` |
| Settings | `#64748b` | `src/components/Layout.tsx:52` |
| Help | `#06b6d4` | `src/components/Layout.tsx:53`, `src/portal/PortalLayout.tsx:40` |
| Follow-ups «پیگیری» / Support (portal) | `#a855f7` | `src/components/Layout.tsx:59`, `src/portal/PortalLayout.tsx:39` |

The chip renders the icon in the hex over `alpha(hex, dark 0.22 / light 0.14)`
(`src/components/Layout.tsx:186`, `src/portal/PortalLayout.tsx:149`).

### 2.5 Color — status & data colors

**Semantic status → MUI Chip `color` prop** (the canonical mechanism — chips inherit
the palette in §2.1):

| Domain | Mapping | Source |
|---|---|---|
| Invoice status | draft→`default`, sent→`info`, paid→`success`, overdue→`warning`, enforced→`error`, canceled→`default` | `src/pages/Invoices.tsx:44`, `src/pages/FinancialHistory.tsx:16-19`, `src/portal/pages/Invoices.tsx:12-19`, `src/pages/Sales.tsx:14` (completed in DS02 — §5-C9 resolved) |
| Payment status | pending→`warning`, confirmed→`success`, rejected→`error` | `src/pages/Payments.tsx:33` |
| Panel sync | ok→`success`, error→`error`, unknown→`info`, disabled→`warning` | `src/pages/Panels.tsx:156-159` |
| Delivery log | sent→`success`, failed/blocked→`error`, unmatched→`warning` | `src/pages/Logs.tsx:14` |
| Enforcement action | dry_run/running→`info`, planned→`default`, partial→`warning`, done→`success`, failed→`error` | `src/pages/Logs.tsx:16-22` |
| Broadcast delivery | sent→`success`, blocked→`warning`, failed→`error`, unregistered/pending→`default` | `src/pages/Broadcast.tsx:50-55` |
| Storefront order | provisioned→`success`, failed→`error`, disabled→`warning`, else `default` | `src/portal/storefront/CustomerDetailPage.tsx:42-45` |
| Storefront campaign | completed→`success`, canceled→`warning`, running→`info`, else `default` | `src/portal/storefront/StorefrontCampaignsPage.tsx:27-28` |
| Portal notification severity | info/success/warning/error → same palette color, rendered as a 3px inline-start edge + `alpha(color, 0.05)` bg | `src/portal/NotificationsBell.tsx:13-15,66-68` |
| Reseller follow-up segment | `color` × `variant` — suspended→`error`/filled, frozen→`error`/outlined, debtor→`warning`/filled, churned→`warning`/outlined, never_active→`default`/filled, dormant→`default`/outlined, onboarding→`info`/filled, declining→`info`/outlined, healthy→`success`/filled, growing→`success`/outlined | `src/pages/Followups/segments.tsx` |

The follow-up board is the one place with **more statuses than palette colors**: MUI's
`color` prop offers six visually distinct values in this theme (`secondary` resolves to the
same `#ff9500` as `warning`) and the board has ten mutually exclusive segments. Pairing each
color with `filled` / `outlined` covers all ten without introducing a single hex — filled
reads as the more urgent half of each pair. Per-segment accent hexes are allowed only on the
summary `StatCard` row, which already takes `navPalette` values.

**StatusPill** (resellers) is palette-derived since DS02 (`v1.100.6`):
`statusPillColors(palette)` in `src/themeTokens.ts` — active → `success`,
muted → `text.secondary`, enforced → `error.main`, frozen → `warning`; light mode
uses the MUI-computed `.dark` variants of success/warning because the pill renders
12px/750 text in the color itself (contrast on light glass). Pill alphas (§3.14)
unchanged. Consumed by `src/pages/resellers/ResellerIdentity.tsx` (ConnectionStatus /
EnforcementStatus).

**Dashboard donut** is palette-derived since DS02 (`v1.100.6`):
`invoiceStatusColor(palette, status)` in `src/themeTokens.ts` — paid→`success.main`,
sent→`info.main`, overdue→`warning.main`, enforced→`error.main`, fallback→
`text.secondary`. Guarded by `src/test/theme-contract.test.ts`. (Historical hexes
`#34d399/#60a5fa/#f7a928/#fb7185/#94a3b8` were removed — §5-C8.)

**Ranking bars** `RANK_COLORS = ["#0071e3", "#30d158", "#ff9500", "#32ade6", "#bf5af2"]`
(`src/pages/Dashboard.tsx:33`) — the app's categorical chart palette, in order.

**StatCard accents in use:** admin dashboard `#0071e3`, `#0ea5e9`, `#10b981`, `#f43f5e`
(`src/pages/Dashboard.tsx:325,332,339,346`); portal dashboard `#10b981`, `#0071e3`,
`#f43f5e`, `#0ea5e9` (`src/portal/pages/Dashboard.tsx:139,146,153,160`); storefront
`#10b981`, `#0ea5e9` (was violet `#7c5cff` — fixed DS05/§5-C1), `#0071e3`, `#f43f5e`
(`src/portal/storefront/StorefrontDashboardPage.tsx`). StatCard default
accent `#0071e3` (`src/components/StatCard.tsx:6`).

**Brand-external color:** Telegram icon `#229ED9` (`src/components/TelegramLink.tsx:32`).

**Portal payment step-dots** use palette colors + `alpha(text.secondary, 0.3)` idle
(`src/portal/pages/Payments.tsx:25-46`, 9px dots).

### 2.6 Color — charts (ECharts)

| Token | Light | Dark | Source |
|---|---|---|---|
| Tooltip bg (via `chartTooltip(theme)` since DS04/D4) | `rgba(255,255,255,0.88)` | `rgba(28,28,30,0.92)` | `src/components/chartTooltip.ts` — consumed by Dashboard + dailyTrend + monthlyTrend (§5-C2/G4 resolved) |
| Tooltip border | `rgba(0,0,0,0.05)` | `rgba(255,255,255,0.14)` | `src/components/chartTooltip.ts` |
| Tooltip text | `theme.palette.text.primary` (both modes) | | `src/components/chartTooltip.ts` |
| Chart font | `CHART_FONT` (= `"Vazirmatn, sans-serif"`) in `src/themeTokens.ts` (DS08) | | consumed by Dashboard, dailyTrend, monthlyTrend, chartTooltip |
| Axis label color / size | `theme.palette.text.secondary`, 11px (9px compact) | | `src/pages/Dashboard.tsx:183,190`, `src/portal/dailyTrend.ts:41,49` |
| Axis line | `alpha(text.secondary, 0.25)` | | `src/pages/Dashboard.tsx:181` |
| Grid split line | `theme.palette.divider` | | `src/pages/Dashboard.tsx:193` |
| Bar fill | vertical gradient: accent → `alpha(accent, light 0.45 / dark 0.35)`; hover bottom stop `alpha(accent, 0.7)` | | `src/pages/Dashboard.tsx:199-218`, `src/portal/dailyTrend.ts:59-65` |
| Bar top radius | `[6,6,0,0]` (compact `[4,4,0,0]`) | | `src/pages/Dashboard.tsx:201`, `src/portal/dailyTrend.ts:58`, `src/portal/monthlyTrend.ts:57` |
| Bar max width | 18 (daily), 12 (compact), 34 (monthly) | | `src/pages/Dashboard.tsx:198`, `src/portal/dailyTrend.ts:56`, `src/portal/monthlyTrend.ts:55` |
| Donut | radius `["66%","84%"]`, item borderRadius 7, borderWidth 3 in `background.paper` | | `src/pages/Dashboard.tsx:230-240` |

Chart runtime: hand-rolled adapter `src/components/EChart.tsx` — canvas renderer,
tree-shaken modules (Bar, Pie, Grid, Tooltip, Legend; `src/components/EChart.tsx:10-14`),
ResizeObserver-driven resize, `setOption(…, { notMerge: true, lazyUpdate: true })`,
default height 300 (`src/components/EChart.tsx:16-40`).

### 2.7 Typography

**Family:** `"Vazirmatn, system-ui, -apple-system, sans-serif"` (`src/theme.ts:122`).
Loaded as a self-hosted **variable font, weights 100–900**, `font-display: swap`,
preloaded (`index.html:25-42`). Identifiers (invoice numbers, TXIDs, UUIDs, links,
secrets) render in `fontFamily: "monospace"` (18 occurrences, e.g.
`src/pages/Invoices.tsx:424`, `src/pages/Payments.tsx:270,274`,
`src/portal/PayDialog.tsx:35`, `src/pages/AccountBackup.tsx:266`). Chart text uses
`"Vazirmatn, sans-serif"` (§2.6).

**Theme scale overrides** (`src/theme.ts:121-129`): h4 `700 / -.02em`,
h5 `700 / -.015em`, h6 `700 / -.01em`, subtitle1 `600`, subtitle2 `600`, button `600`.
All other variants are MUI defaults.

**Weight vocabulary in use** (census across `src`): 500 (unselected nav/tab), 600
(labels, secondary emphasis), 650 (metric detail, `src/pages/Dashboard.tsx:58`), 700
(bold body/values), 750 (row titles, StatusPill, prices), 800 (page/section headers,
brand), 850 (StatCard values, hero numbers, logo text). Canonical roles: **500 / 600 /
700 / 800** as the base ramp, with 650/750/850 as the "half-step" emphasis ramp already
established across pages (kept — it is the majority pattern; see §5-G6).

**Font-size vocabulary in use** (px unless noted; all cited values exist):
9 & 11 chart axis (`src/portal/dailyTrend.ts:41`), 11 bottom-nav labels
(`src/components/BottomNav.tsx:56`), 11.5 fine print (`src/pages/Login.tsx:350,370`),
12 tooltip text (`src/theme.ts:464`) / StatusPill (`ResellerIdentity.tsx:32`) / badges
(`TopupsPage.tsx:543`), 12.5 table head (`src/theme.ts:396`) / resp-table cell label
(`src/theme.ts:200`) / StatCard sub (`StatCard.tsx:43`) / monospace links
(`src/portal/pages/Panels.tsx:110`), 13 login alert (`Login.tsx:207`) / copy rows
(`PayDialog.tsx:35`), 13.5 SegmentedTabs (`SegmentedTabs.tsx:60`), 14 body-ish
(`Login.tsx:360`, portal helper text), 14.5 nav labels
(`Layout.tsx:204`, `PortalLayout.tsx:161`), 15–17 inline text-adjacent icons
(15 `src/pages/Dashboard.tsx:253`, 16 `src/portal/PayDialog.tsx:48`, 17 ×12 e.g.
`src/portal/pages/Panels.tsx:76`), 15.5 login submit (`Login.tsx:340`), 18 pay-dialog
amount emphasis (`src/portal/PayDialog.tsx:165`), 19 nav-chip icon
glyphs (`Layout.tsx:196`) & login brand (`Login.tsx:414`), 23 StatCard icon glyphs
(`StatCard.tsx:64`) & login h1 xs (`Login.tsx:192`), 25 login h1 sm, 27 donut center
(`Dashboard.tsx:453`), 36 help hero icon (`Help.tsx:325`), 48 success/error hero icons
(`Setup.tsx:58`, `ErrorBoundary.tsx:41`), `{1.35rem, 1.65rem}` StatCard value xs/sm
(`StatCard.tsx:37`).

**Line-heights:** 1 (pills/hero numbers), 1.1 (brand block, `Layout.tsx:154`), 1.2
(StatCard value, `StatCard.tsx:36`), 1.9 (long Persian body copy, 5×, e.g.
`src/pages/Help.tsx` secondary, `src/portal/PortalEntry.tsx:155`), `"20px"` (badge
`lineHeight: "20px"`, `TopupsPage.tsx:543`). Letter-spacing: only the heading
negatives + table-head `.01em` (`src/theme.ts:397`).

### 2.8 Radii

Base token `shape.borderRadius = 14` (`src/theme.ts:130`). Remember: numeric `sx`
radii are ×14 multipliers (§ preamble).

| Step | Value | Used for | Source |
|---|---|---|---|
| focus | 4px | `:focus-visible` ring corners | `src/theme.ts:213` |
| hairline | 2px | Tabs indicator | `src/theme.ts:453-454` |
| micro | 3–4px | progress bars (3: `CapacityBar.tsx:24`, `Subs.tsx:42`; 4: theme LinearProgress `src/theme.ts:547`, storefront plan bars `StorefrontDashboardPage.tsx:109`), nav accent bar 3px (`Layout.tsx:96`) | |
| small | 8px | scrollbar thumb (`src/theme.ts:140`), Tab (`src/theme.ts:432`), Tooltip (`src/theme.ts:466`), QR border (`AccountBackup.tsx:262`), tree connector corner (`ResellerIdentity.tsx:96`) | |
| icon | 10px | IconButton (`src/theme.ts:526`), ListItemButton (`src/theme.ts:535`), login logo tile (`Login.tsx:405`) | |
| switch | 11px | Switch track | `src/theme.ts:541` |
| alert | 12px | Alert (`src/theme.ts:487`), login secondary button (`Login.tsx:360`), login alert (`Login.tsx:206`) | |
| **base** | **14px** | shape default; Menu/Popover (`src/theme.ts:236,241`), Snackbar (`src/theme.ts:503`), Accordion (`src/theme.ts:516`), multiline input (`src/theme.ts:312`), resp-table row-card (`src/theme.ts:183`), StatCard icon tile `"14px"` (`StatCard.tsx:52`), login fields/captcha/submit `"14px"` (`Login.tsx:30,271,339`) | |
| card | 18px | MuiCard | `src/theme.ts:223` |
| dialog | 20px | MuiDialog (0 when fullScreen) | `src/theme.ts:244-252` |
| hero | 24px | login glass card `"24px"` | `src/pages/Login.tsx:156` |
| sx-2 | 28px | icon chips 31px (`Layout.tsx:182`), rank tiles 30px (`Dashboard.tsx:534`), inset boxes (`PayDialog.tsx:34`, `portal/pages/Panels.tsx:69`) | |
| sx-2.5 | 35px | nav items (`Layout.tsx:71`), logo tile 40px (`Layout.tsx:140`) | |
| sx-3 | 42px | mobile row-cards (`ResellerMobileCard.tsx:32`, `Invoices.tsx:444`, `Payments.tsx:291`), portal-entry logo 56px (`PortalEntry.tsx:134`), Help accordions (`Help.tsx:259`), Settings sticky header (`Settings.tsx:602`), phone-preview card `{xs:3, sm:5}` (`StorefrontPreviewPage.tsx:29`) | |
| pill (inputs) | `"50px"` | single-line OutlinedInput (`src/theme.ts:316`), SegmentedTabs container+tabs (`SegmentedTabs.tsx:33,57`), LiveRate (`LiveRate.tsx:41`) | |
| pill (controls) | 980 | Button all variants+sizes (`src/theme.ts:341,354,363,366-367`), Chip (`src/theme.ts:374`) | |
| pill (badges/meters) | 99 / 999 | StatusPill 99 (`ResellerIdentity.tsx:27`), dashboard meter bars 99 (`Dashboard.tsx:394,556`), count badges 999 (`StorefrontShell.tsx:134`, `TopupsPage.tsx:540`) | |
| circle | `"50%"` | status dots (`ResellerIdentity.tsx:38`, `Dashboard.tsx:483`, `portal/pages/Payments.tsx:40`) | |

Pill spelling is inconsistent (50px / 980 / 99 / 999) — one intent, four values → §5-C6.

### 2.9 Elevation & shadows

The composite glass shadows (ring + depth + specular rim):

| Token | Light | Dark | Source |
|---|---|---|---|
| Tier‑1 `glassShadow` | `0 0 0 0.5px rgba(0,0,0,0.05), 0 2px 20px rgba(0,0,0,0.06), inset 0 1px 0 rgba(255,255,255,1.0)` | `0 0 0 0.5px rgba(255,255,255,0.10), 0 2px 20px rgba(0,0,0,0.50), inset 0 1px 0 rgba(255,255,255,0.08)` | `src/theme.ts:36-46` |
| Tier‑2 `floatShadow` | `0 0 0 0.5px rgba(0,0,0,0.08), 0 12px 40px rgba(0,0,0,0.14), inset 0 1px 0 rgba(255,255,255,1.0)` | `0 0 0 0.5px rgba(255,255,255,0.14), 0 12px 40px rgba(0,0,0,0.70), inset 0 1px 0 rgba(255,255,255,0.10)` | `src/theme.ts:48-58` |
| AppBar ring | `0 0 0 0.5px rgba(0,0,0,0.08)` | `0 0 0 0.5px rgba(255,255,255,0.08)` | `src/theme.ts:278-280` |
| Tab selected | `0 0 0 0.5px rgba(0,0,0,0.06), 0 2px 8px rgba(0,0,0,0.08)` | `0 0 0 0.5px rgba(255,255,255,0.12), 0 2px 8px rgba(0,0,0,0.40)` | `src/theme.ts:439-441` |
| Tooltip | `0 4px 16px rgba(0,0,0,0.12)` | `0 4px 16px rgba(0,0,0,0.60)` | `src/theme.ts:475-477` |
| Snackbar | `0 8px 32px rgba(0,0,0,0.12)` | `0 8px 32px rgba(0,0,0,0.60)` | `src/theme.ts:504-506` |
| Alert rim | `inset 0 1px 0 rgba(255,255,255,0.90)` | `inset 0 1px 0 rgba(255,255,255,0.06)` | `src/theme.ts:489-491` |
| Contained-primary button | `0 4px 14px alpha(primary,0.40)`; hover `0 6px 20px alpha(primary,0.55)` | same | `src/theme.ts:349-352` |
| Tabs indicator glow | `0 0 8px 1px alpha(primary,0.50)` | same | `src/theme.ts:455` |
| Nav item selected | `inset 0 1px 0 rgba(255,255,255,.96), 0 2px 12px -6px rgba(0,0,0,.18)` (neutralized DS04) | `inset 0 1px 0 rgba(255,255,255,.14), 0 2px 12px -6px rgba(0,0,0,.45)` | `src/components/Layout.tsx:82-84`, `src/portal/PortalLayout.tsx:71-73` |
| Nav accent bar glow | `0 0 8px 2px alpha(primary,0.42)` | same | `src/components/Layout.tsx:98` |
| Icon chip selected | `0 0 0 1px alpha(c,0.50), 0 4px 12px -4px alpha(c,0.55), inset 0 1px 0 rgba(255,255,255,.32)`; resting `inset 0 1px 0 rgba(255,255,255,.18)` | same | `src/components/Layout.tsx:188-194` |
| Logo tile | `0 6px 18px -6px rgba(0,113,227,.55), inset 0 1.5px 0 rgba(255,255,255,.40)` | same | `src/components/Layout.tsx:145-148`, `src/pages/Login.tsx:408`, `src/portal/PortalLayout.tsx:115` |
| Portal-entry logo | `0 8px 22px -8px rgba(0,113,227,.6)` | same | `src/portal/PortalEntry.tsx:137`, `src/portal/PortalLogin.tsx:93` |
| StatCard hover | `0 10px 28px -12px alpha(accent,0.45)` light / `0 8px 26px -10px alpha(accent,0.5)` dark | | `src/components/StatCard.tsx:18-20` |
| SegmentedTabs selected | `inset 0 1px 0 rgba(255,255,255,.92)` + `0 2px 10px -4px alpha(primary,.30)` | `inset 0 1px 0 rgba(255,255,255,.16)` + `0 2px 10px -4px alpha(primary,.45)` | `src/components/SegmentedTabs.tsx:69-74` |
| Captcha box / refresh button (login) | box bg `rgba(255,255,255,0.92)` both modes, border `rgba(255,255,255,.80)`, rim `inset 0 1px 0 rgba(255,255,255,.96)`; refresh border `rgba(255,255,255,.72)`, bg `rgba(255,255,255,.42)` → hover `rgba(255,255,255,.70)` | refresh border `rgba(255,255,255,.12)`, box border `rgba(255,255,255,.14)`, bg `rgba(255,255,255,.04)` → hover `rgba(255,255,255,.08)` | `src/pages/Login.tsx:262-314` |
| Login hero card | light `0 32px 80px -24px rgba(0,0,0,.26), inset 0 1.5px 0 rgba(255,255,255,.98), inset 0 -1px 0 rgba(0,0,0,.04), 0 0 0 0.5px rgba(0,0,0,0.08)` (neutralized DS04); dark `0 32px 80px -24px rgba(0,0,0,.88), inset 0 1.5px 0 rgba(255,255,255,.22), inset 0 -1px 0 rgba(0,0,0,.18), 0 0 0 0.5px rgba(255,255,255,.07)` | | `src/pages/Login.tsx:169-182` |
| Settings header | light `0 8px 24px -16px rgba(0,0,0,.22)` (neutralized DS04) / dark `0 8px 24px -14px rgba(0,0,0,.65)` | | `src/pages/Settings.tsx` sticky header |
| Sidebar inner edge | light `inset -1px 0 0 rgba(255,255,255,.60)` / dark `inset -1px 0 0 rgba(255,255,255,.05)` | | `src/components/Layout.tsx:250-252` |
| Login illustration | `filter: drop-shadow(0 28px 30px rgba(0,0,0,.14))` (neutralized DS04) | | `src/pages/Login.tsx:431` |

MUI elevation shadows are suppressed where glass applies (`disableElevation` buttons
`src/theme.ts:338`, `Paper backgroundImage: none` + `elevation0` `src/theme.ts:228-233`,
AppBar `elevation={0}` `src/components/Layout.tsx:288`).

### 2.10 Blur / saturation tiers

*(Rewritten by DS01/DS03 — §5-C5 resolved. The canonical trio is now the ONLY set in
code, exported from `src/themeTokens.ts` and pinned by `src/test/theme-contract.test.ts`.)*

| Tier | Recipe | Used by | Source |
|---|---|---|---|
| Chrome `CHROME_BLUR` | `blur(48px) saturate(220%) brightness(1.03)` | desktop sidebars, mobile Drawer (DS01), login hero card (DS03) | `src/themeTokens.ts`; `src/components/Layout.tsx`, `src/portal/PortalLayout.tsx`, `src/pages/Login.tsx` |
| Tier‑2 `TIER2_BLUR` | `blur(40px) saturate(180%)` | Menu, Popover, Dialog, AppBar, Tooltip, Alert, Snackbar, BottomNav, selected Tab (DS01), selected nav item, SegmentedTabs, Settings sticky header, login captcha box + refresh (DS03) | `src/themeTokens.ts`; theme + component sites |
| Tier‑1 `TIER1_BLUR` | light `blur(40px) saturate(180%)` / dark `blur(20px) saturate(140%)` | Cards, Accordions, resp-table row-cards, Settings section + side-nav Papers (DS03) | `src/themeTokens.ts`; `src/theme.ts` glassBlur + resp-table, `src/pages/Settings.tsx` |

Historical drift (removed): hero `…brightness(1.04)`, Settings `28/200/1.02` and
`14/180`, nav/captcha `16/180`, SegmentedTabs `12/180`, refresh `12/160`, Tab `20/180`.
Every `backdropFilter` is paired with `WebkitBackdropFilter`.

### 2.11 Borders & dividers

Hairline `1px solid` everywhere; the glass border colors are §2.2 `glassBorder`.
Special cases: light-mode "white borders" on chrome — sidebar
`rgba(255,255,255,.75)` (`src/portal/PortalLayout.tsx:190`; admin same file pattern
`Layout.tsx:249`), nav selected `rgba(255,255,255,.78)` (`Layout.tsx:85`), login card
`rgba(255,255,255,.82)` (`Login.tsx:168`), captcha `rgba(255,255,255,.80)`
(`Login.tsx:270`), captcha refresh `rgba(255,255,255,.72)` (`Login.tsx:301`); table head
underline `rgba(0,113,227,0.14)` light / `rgba(41,151,255,0.18)` dark
(`src/theme.ts:398`); mobile tree emphasis `borderInlineStartWidth: 3` in
`primary.main` (`ResellerMobileCard.tsx:36-37`); notification edge `3px solid` severity
color (`NotificationsBell.tsx:68`); QR border `rgba(0,0,0,0.12)`
(`AccountBackup.tsx:262`, neutralized DS04); resp-table inner row divider
`rgba(0,0,0|255,255,255,0.06)` (`src/theme.ts:195`).

### 2.12 Z-index

No custom scale — MUI defaults, plus: ambient layer `-1` (`src/theme.ts:156`),
sidebar glow `0` / content `1` (`Layout.tsx:129,133`, `PortalLayout.tsx:104,107`),
login zones `2` (`Login.tsx:145,394`), Settings sticky header `3`
(`Settings.tsx:601`), BottomNav `theme.zIndex.appBar` (`BottomNav.tsx:35`).

### 2.13 Motion

| Token | Value | Source |
|---|---|---|
| Standard ease | `EASE_ENTRANCE` tuple + `ENTRANCE_BEZIER` string (= `cubic-bezier(.22,1,.36,1)`) in `src/themeTokens.ts` (DS08) | consumed by motion.tsx, Dashboard, theme keyframes, Login |
| Spring (overshoot) | `SPRING_BEZIER` (= `cubic-bezier(.34,1.56,.64,1)`) in `src/themeTokens.ts` (DS08) | theme buttons/icon-buttons, both layouts' nav-icon transitions |
| Micro state transitions | `.14s` (button/icon transform), `.15s` (input shadow/border, row bg, ListItemButton), `.18s` (tab/nav bg, select icon), `.2s` (card shadow, bg-color, misc `"0.2s"` `Settings.tsx:582`) | `src/theme.ts:224,297,317,345,412,433,527,535`, `src/components/Layout.tsx:105` |
| Row entrance | `rowIn .36s` both + stagger `i*28ms` for rows 1–14 | `src/theme.ts:96-101,161-173` |
| Page entrance | fade+rise 14px, `.4s` EASE (`PageTransition`); `Reveal` 16px `.45s` (+delay) | `src/components/motion.tsx:12-22,47-57` |
| Login card entrance | `glassIn .55s` (scale .97→1, rise 8px) | `src/theme.ts:165-168`, `src/pages/Login.tsx:184` |
| Dashboard staggers | stat cards `.45s`, delay `index*0.07`; meter bars width anim `.65s`, delay `.15 + index*0.06` | `src/pages/Dashboard.tsx:352-354,401-404` |
| CountUp | numbers animate 0→value over `1.2s` EASE | `src/components/motion.tsx:29-44` |
| Hover lifts | buttons `translateY(-1px)` + opacity .90; StatCard `translateY(-2px)`; active press `scale(.97)`; IconButton hover `scale(1.10)` / active `scale(.94)`; nav icon `scale(1.14) rotate(-3deg)` | `src/theme.ts:346-347,528-529`, `src/components/StatCard.tsx:16`, `src/components/Layout.tsx:104` |
| Reduced motion | all animations/transitions forced to `0.001ms` | `src/theme.ts:206-208`; CountUp snaps (`src/components/motion.tsx:32-37`) |
| Toast auto-hide | 4500ms | `src/components/Toast.tsx:16` |

### 2.14 Breakpoints, layout & spacing

- Breakpoints: **MUI defaults** (xs 0, sm 600, md 900, lg 1200, xl 1536) — not
  overridden (`src/theme.ts:103`). Usage census: `down("md")` mobile-card/table switch
  + Settings compact (4×, e.g. `src/pages/Invoices.tsx:51`, `src/pages/Settings.tsx:391`),
  `up("md")` desktop sidebar (2×, `src/components/Layout.tsx:59`), `down("sm")` →
  full-screen dialogs (`src/responsive.ts:5-8`). Raw CSS mobile query
  `@media (max-width:599.95px)` (`src/theme.ts:176`).
- Layout constants: sidebar **256px** for BOTH apps via `SIDEBAR_WIDTH`
  (`src/themeTokens.ts`, consumed by `src/components/Layout.tsx` +
  `src/portal/PortalLayout.tsx` — §5-C10 resolved DS05); Settings side nav
  240px sticky top 80 (`src/pages/Settings.tsx:623`); content padding
  `p: { xs: 2, md: 3 }` (16/24px) + mobile bottom clearance
  `calc(76px + env(safe-area-inset-bottom))` (`src/components/Layout.tsx:314`,
  `src/portal/PortalLayout.tsx:240` without the clearance); BottomNav height 60
  (`src/components/BottomNav.tsx:48`); login split `46% / 54%` at md
  (`src/pages/Login.tsx:127`); auth card max-widths 520 (login `Login.tsx:154`),
  480/460 (setup `Setup.tsx:93,56`), 420 (portal entry `PortalEntry.tsx:129`).
- Spacing: default **8px** MUI unit (not overridden). Recurring rhythm: page header
  `mb: 2/2.5`, grid `spacing={2}`, card content `p: { xs: 2, sm: 2.5 }` with
  `&:last-child pb` equalized (`src/components/StatCard.tsx:27`,
  `src/portal/ui.tsx:18`), stacked forms `Stack spacing={2}`, dense chips
  `spacing={0.8}`, mobile card list `spacing={1.2}` in `p: 1.5`
  (`src/pages/Invoices.tsx:441`).
- Safe-areas: `viewport-fit=cover` (`index.html:5`), AppBar
  `pt: env(safe-area-inset-top)` (`src/components/Layout.tsx:289`), BottomNav
  `pb: env(safe-area-inset-bottom)` (`src/components/BottomNav.tsx:41`).

### 2.15 Ambient backgrounds (beyond the body glow)

- Sidebar top glow — **one token since DS05 (§5-C1 resolved):** both layouts consume
  `SIDEBAR_AMBIENT` from `src/themeTokens.ts` — Apple blue
  `radial-gradient(ellipse 120% 80% at 50% 0%, rgba(0,113,227,.26) 0%, transparent
  70%)` dark / `rgba(0,113,227,.18)` light. The admin violet (`rgba(139,92,246,…)`)
  is gone.
- Portal entry/login page wash: `radial-gradient(ellipse 80% 60% at 70% 0%,
  rgba(0,113,227,.14) 0%, transparent 60%)` (`src/portal/PortalEntry.tsx:125-126`,
  `src/portal/PortalLogin.tsx:81-82`).
- Help hero card: `linear-gradient(135deg, alpha(primary, dark .28 | light .09) 0%,
  transparent 70%)` (`src/pages/Help.tsx:321`, `src/portal/pages/Help.tsx:139-144`).
- StatCard corner wash: `radial-gradient(120% 120% at 100% 0%, alpha(accent, dark .16 |
  light .08) 0%, transparent 48%)` (`src/components/StatCard.tsx:24-26`).
- Gloss overlays on tiles/chips: `linear-gradient(145deg, rgba(255,255,255,.24) 0%,
  rgba(255,255,255,0) 60%)` on nav icon chips (`src/components/Layout.tsx:187`) and the
  same at `rgba(255,255,255,.28)` on StatCard tiles (`src/components/StatCard.tsx:56`);
  nav selected gloss `linear-gradient(175deg, rgba(255,255,255,.12) 0%,
  rgba(255,255,255,0) 60%)` (`src/components/Layout.tsx:81`); SegmentedTabs selected
  gloss `…(175deg, rgba(255,255,255,.20) …)` (`src/components/SegmentedTabs.tsx:68`);
  login card sheen — dark `linear-gradient(175deg, rgba(255,255,255,.07) 0%,
  rgba(255,255,255,.01) 40%, rgba(0,0,0,.06) 100%)`, light `linear-gradient(175deg,
  rgba(255,255,255,.52) 0%, rgba(255,255,255,.07) 42%, rgba(0,0,0,.02) 100%)`
  (`src/pages/Login.tsx:161-164`).
- Brand gradients: logo tile `linear-gradient(145deg, #5ab5ff 0%, #0071e3 100%)`
  (`src/components/Layout.tsx:144`, `src/pages/Login.tsx:407`,
  `src/portal/PortalLayout.tsx:114`, `src/portal/PortalEntry.tsx:136`,
  `src/portal/PortalLogin.tsx:92`); nav accent bar `linear-gradient(180deg,
  alpha(primary,0.9), alpha(primary,0.55))` (`src/components/Layout.tsx:97`); meter
  bars `linear-gradient(90deg, alpha(color,0.72), color)` (`src/pages/Dashboard.tsx:408`).

### 2.16 Static assets & PWA chrome

- Favicon/icon: receipt glyph, rounded 14/64 tile, gradient `#5ab5ff → #0064d2`, gloss
  `rgba(255,255,255,0.28)→0` top half, paper `rgba(255,255,255,0.95)`, detail lines
  `#0071e3` at opacity 0.7/0.35, check badge `#0071e3` (+15% white overlay), white
  2.2-width check stroke (`public/favicon.svg`, full-bleed `public/icon-square.svg`).
  Note the icon gradient ends at `#0064d2` while the in-app logo tile ends at `#0071e3`
  → §5-C11.
- Manifest: `background_color #000000`, `theme_color #0071e3`, `dir rtl`, standalone
  (`public/site.webmanifest:6-10`).
- `<meta name="theme-color">`: `#f5f5f7` light / `#000000` dark + JS sync
  (`index.html:18-20`, `src/main.tsx:24-28`).
- iOS standalone: `black-translucent` status bar (`index.html:9`).
- `public/login.svg` (1.9 MB) is a licensed illustration; its internal palette
  (greys/gold `#f3d257` family) is **artwork, not design tokens** — excluded from the
  token set by rule. Its only styling contract is the drop-shadow applied at
  `src/pages/Login.tsx:431`.
- `frontend/placeholder.html` — deleted in DS08 (§5-G9 resolved).

---

## §3 Component specs

Format: anatomy → states → exact tokens. Components inherit §2 unless noted.

### 3.1 Buttons (`src/theme.ts:336-369`)

- **Shared:** radius 980 pill, `textTransform: none`, weight 600,
  `paddingInline: 20` (small 14, large 28), `disableElevation`. Transition
  `transform .14s spring, opacity .2s, background-color .2s`. Hover
  `translateY(-1px)` + `opacity: 0.90`; active `translateY(0) scale(.97)`.
  Disabled: MUI default (no override).
- **Contained (primary):** accent glow `0 4px 14px alpha(primary,.40)` → hover
  `0 6px 20px alpha(primary,.55)` (`src/theme.ts:349-352`).
- **Outlined:** frosted — border `rgba(0,0,0,0.15)`|`rgba(255,255,255,0.20)`, bg
  `rgba(255,255,255,0.70)`|`rgba(255,255,255,0.05)`; hover deepens both
  (`src/theme.ts:353-361`).
- **Text:** hover `alpha(primary, light .07 / dark .12)` (`src/theme.ts:362-365`).
- Deviation: Login submit/passkey use radius `"14px"`/`"12px"`, heights 54/46 → §5-C4.

### 3.2 Text inputs & Select (`src/theme.ts:285-334`)

- **Shape:** single-line = `50px` pill; multiline = 14px; input text ellipsizes
  (`src/theme.ts:305-316`).
- **States:** resting/hover/focus backgrounds and outline colors per §2.3; focus adds
  ring `0 0 0 3px alpha(primary, 0.22)` (`src/theme.ts:327`). Transition
  `box-shadow .15s, border-color .15s, background-color .2s`.
- **Select:** `paddingBlock 7px`, `paddingInlineStart 16px`; icon in §2.3 color with
  `transform .18s` (`src/theme.ts:286-300`); compact `size="small"` +
  `py: 7px !important` in PeriodPicker (`src/components/PeriodPicker.tsx:27-30`).
- **Disabled/error states:** MUI defaults are canonical (D2) — no custom styling;
  set only the `error`/`disabled` props.
- **RTL/LTR rule:** see §4.2 (hard rule). Login uses placeholder-only fields, height
  58, radius `"14px"`, explicit `inputProps={{ dir: "rtl", textAlign right }}`
  (`src/pages/Login.tsx:27-41`) → logged deviation §5-C4.

### 3.3 Card & SectionCard

- **MuiCard** = tier‑1 glass + radius 18 + `transition: box-shadow .20s, transform .20s`
  (`src/theme.ts:219-227`). TableContainer inside stays transparent
  (`src/theme.ts:381-386`).
- **SectionCard** (titled card): header row `px {xs:2, sm:2.5} / py 1.8`, bottom
  divider, title `subtitle1` weight 800, optional trailing action; content
  `p {xs:2, sm:2.5}` with `&:last-child` pb equalized. Duplicated implementations:
  `src/pages/Dashboard.tsx:65-98` and `src/portal/ui.tsx:4-23` → §5-C13.

### 3.4 StatCard (`src/components/StatCard.tsx`)

Anatomy: label (`body2`, secondary, 600) → value (`fontSize {1.35rem/1.65rem}`, weight
850, `mt 1.4`, nowrap) → optional sub (12.5, `minHeight 21`) + trailing 44px icon tile
(radius `"14px"`, `alpha(accent,0.15)` bg, 145° gloss, `alpha(accent,0.25)` border,
rim+glow shadow, glyph 23). Corner accent wash per §2.15. Hover: lift −2px, border
`alpha(accent, dark .5 / light .35)`, glow shadow §2.9. Values animate via `CountUp`.
Default accent `#0071e3`.

### 3.5 Dialogs (`src/theme.ts:244-252` + usage conventions)

Tier‑2 glass, radius 20 (0 fullscreen). Conventions: `fullWidth` +
`maxWidth="xs"` for forms / `"md"` for detail views; `fullScreen={useXsFullScreen()}`
on phones (`src/responsive.ts:5-8`, e.g. `src/pages/Invoices.tsx:498,521,548`).
Action order: cancel («انصراف») first as text, primary action last as `contained`;
destructive in-dialog actions use `color="error"` (`src/pages/Invoices.tsx:512-516,
539-543`). Scrollable inner lists: `maxHeight {xs none / sm 360}` + `flex: 1` on xs
(`src/pages/Invoices.tsx:563-574`).

### 3.6 Menus & Popovers

Tier‑2 glass, radius 14 (`src/theme.ts:234-243`). Notifications popover width 340 /
`92vw` (`src/portal/NotificationsBell.tsx:53`). Row-action menus close before invoking
handlers (`src/components/RowActionsMenu.tsx:54-57`).

### 3.7 Tooltip

Glass tooltip: fontSize 12, weight 500, radius 8, padding 6/10, per-mode bg 0.92, noise,
1px glass border, mode shadow; arrow matches bg (`src/theme.ts:461-481`). Every
icon-only control gets a Persian `Tooltip` + `aria-label`; disabled buttons are wrapped
in `<span>` to keep tooltips alive (`src/components/RowActionsMenu.tsx:30-37`,
`src/components/LiveRate.tsx:61-67`).

### 3.8 Chip

Weight 600, radius 980; outlined variant frosted per §2.3 (`src/theme.ts:372-380`).
Usage grammar: `size="small"` everywhere in tables; status chips use the semantic
`color` prop maps (§2.5); metadata chips (panel key, period) use `variant="outlined"`;
counters may use `sx={{ fontWeight: 700/750 }}` (`src/pages/Dashboard.tsx:368,431`).

### 3.9 Switch & progress

Switch: `padding 8`, track radius 11, opacity .28 light / .35 dark
(`src/theme.ts:538-543`). LinearProgress: height 6, radius 4, track per §2.3
(`src/theme.ts:544-552`); CapacityBar overrides height 6 / radius 3; portal Subs 7/3;
storefront plan bars 8/4 — all removed in DS07; every LinearProgress now inherits the theme geometry
(§5-C12 resolved).

### 3.10 CapacityBar (`src/components/CapacityBar.tsx`)

Anatomy: caption row (`used/max` LTR, percent right) + determinate LinearProgress,
`minWidth 92`. Thresholds: `info` <70%, `warning` ≥70%, `error` ≥90%
(`src/components/CapacityBar.tsx:10-12`). No limit → `∞` label, value 0, opacity 0.3.
Tooltip `{pct}% پر شده` / «بدون سقف».

### 3.11 Tables (`src/theme.ts:381-423` + page conventions)

- Container: transparent (glass comes from the wrapping Card); desktop scroll bound
  `maxHeight: calc(100vh - 300px)` + `stickyHeader` (`src/pages/Invoices.tsx:398-399`).
- Head cells: **opaque** blue-tinted surface `#eef4fc`/`#20262f`, text
  `#0064c8`/`#6aadff`, weight 700, size 12.5, letter-spacing .01em, accent underline,
  nowrap (`src/theme.ts:387-403`; opacity rationale in the comment: sticky headers must
  not let rows bleed through).
- Rows: zebra + hover per §2.3, `transition .15s`, last row borderless, entrance
  `rowIn` + 28ms stagger (§2.13). Density: `size="small"`; resellers rows
  `td py: 1.05` (`src/pages/resellers/ResellerTable.tsx:44`); tree rows explicitly
  carry **no** background tint (comment `src/pages/resellers/ResellerTable.tsx:41-43`).
- Sorting: `SortTh` + `useSort` (fa-aware localeCompare, nulls last,
  `src/components/sortable.tsx`); sort icon opacity 0.4 (`src/theme.ts:423`).
- Pagination: `TablePager` — options `[25, 50, 100]`, Persian labels
  `تعداد در صفحه:` / `{from}–{to} از {count}`, hidden when one page
  (`src/components/TablePager.tsx:20-31`); heavy pages inline the same
  `TablePagination` (`src/pages/Invoices.tsx:483-492`).
- Empty rows: full-colspan centered cell, `py: 4`, `text.secondary`, contextual Persian
  copy (`src/pages/Invoices.tsx:435`).
- **Mobile pattern A — explicit cards** (canonical for action-heavy lists): at
  `down("md")` render `Stack spacing={1.2}` in `p: 1.5`; each card `p: 1.5`,
  radius sx‑3, 1px divider border, `bgcolor: nestedCardBg`; header row (title 750 +
  status chip), meta chip row, 2-col key-value grid, bordered footer with
  `RowActionsMenu` (`src/pages/Invoices.tsx:441-475`,
  `src/pages/Payments.tsx:288-328`, `src/pages/resellers/ResellerMobileCard.tsx`).
- **Mobile pattern B — `resp-table`** (canonical for simple/detail tables): add
  `className="resp-table"`; global CSS turns rows into cards (radius 14, §2.2 bg,
  `td` flex rows labeled via `td::before content: attr(data-label)` at 12.5/600) with
  labels auto-copied from `<thead>` by `useResponsiveTableLabels`
  (`src/theme.ts:176-204`, `src/responsive.ts:19-51`). Dual-mechanism status → §5-C3.

### 3.12 Tabs

- **Plain MuiTab/Tabs** (secondary nav): no uppercase, weight 500→700 selected,
  minHeight 40, radius 8, selected = frosted chip + shadow (§2.3/§2.9), indicator
  height 2 + accent glow (`src/theme.ts:425-458`). Used in Help, Settings sidebar
  (vertical, `minHeight 48`, `src/pages/Settings.tsx:624-634`), StorefrontShell
  (`src/portal/storefront/StorefrontShell.tsx:98-106`).
- **SegmentedTabs** (page-level view switcher): `50px` pill container `p: 0.45`,
  divider border, frosted bg + `blur(12px) saturate(180%)`; tabs minHeight 38,
  `px 2 / py 0.7`, 13.5/600; selected = `alpha(primary, .10|.18)` + gloss + rim/glow,
  indicator hidden; scrollable with mobile scroll buttons
  (`src/components/SegmentedTabs.tsx`). Used by Resellers/Invoices switchers.

### 3.13 App chrome

- **AppBar:** sticky, glass per §2.2, `elevation 0`, bottom divider, safe-area top pad;
  contents: burger (mobile), page title `h6` weight 800 (label from NAV), mode toggle
  icon, user Chip (outlined, small; hidden on xs in admin)
  (`src/components/Layout.tsx:286-312`, `src/portal/PortalLayout.tsx:219-238`; theme
  `src/theme.ts:270-283`).
- **Sidebar (admin):** desktop = sticky 256px chrome-glass column; brand block (40px
  gradient logo tile radius sx‑2.5 + name subtitle1/800 + caption), divider, nav list,
  logout (error-tinted hover `alpha(error, 0.08)`), version footer (caption, LTR).
  Nav item: radius sx‑2.5, `mx 1.25 / my 0.3 / py 0.7`, label 14.5 (500→700 selected),
  31px icon chip (radius sx‑2, per-destination accent, gloss, glyph 19); selected state
  adds frosted bg + rim shadow + 1px border + 3px accent gradient bar with glow
  (`insetInlineStart 4, top/bottom 9`); hover scales icon 1.14 rotate −3°
  (`src/components/Layout.tsx:69-233`). Mobile: right-anchored temporary Drawer, same
  content, width 256 (`src/components/Layout.tsx:274-284`).
- **Sidebar (portal):** full parity with admin since DS05 (§5-C10) — 256px via
  `SIDEBAR_WIDTH`, byte-identical `navItemSx` (selected backdrop-filter + gloss +
  selected-hover + hover icon scale), icon chips with `GLOSS_NAV_CHIP` + spring
  transition + selected rim inset (`src/portal/PortalLayout.tsx`). Deliberate
  remaining differences: no version footer (portal API exposes no version endpoint —
  N/A) and no BottomNav (D3 deferred).
- **BottomNav (admin, phones):** fixed, glass, top divider, height 60, safe-area pb;
  4 tabs + «بیشتر» (opens drawer), labels 11 (`src/components/BottomNav.tsx`). Portal
  has none → §5-G3.

### 3.14 Feedback & status components

- **Toast:** `useToast` Snackbar bottom-left, auto-hide 4500ms, **filled** Alert
  severity variant (`src/components/Toast.tsx:13-24`); error extraction via `errMsg`.
- **Alert:** glass, radius 12, glass border, rim inset (`src/theme.ts:482-494`).
  Inline page alerts: error + retry button pattern (`src/components/DataState.tsx:20-33`,
  `src/pages/Dashboard.tsx:280-286`).
- **Skeleton / loading:** wave animation default (`src/theme.ts:553-565`); `DataState`
  renders a Card with one 34px header bar + N 40px rounded rows (radius 1.5)
  (`src/components/DataState.tsx:35-47`); Dashboard uses shaped skeletons matching the
  final layout (154/390/360/300 heights, radius `"18px"`,
  `src/pages/Dashboard.tsx:288-306,594`); route fallback = centered CircularProgress
  (`src/App.tsx:43-49`, `src/components/Layout.tsx:316-321`).
- **Empty states:** centered `body2` in `text.secondary`; wrappers: Dashboard
  `minHeight 210` (`src/pages/Dashboard.tsx:100-114`), portal `minHeight 180`
  (`src/portal/ui.tsx:25-31`), table cells `py: 4/5`. Copy states what's missing and,
  where actionable, what to do (e.g. `src/pages/Invoices.tsx:435`). → §5-C14 for the
  min-height variants.
- **ErrorBoundary screen:** centered Paper `p: 4`, maxWidth 520, 48px error icon,
  h6 title, body2 secondary, LTR caption of the raw error, contained reload button
  (`src/components/ErrorBoundary.tsx:36-56`).
- **StatusPill:** inline pill (radius 99), 7px `currentColor` dot, 12/750 text, tinted
  bg `alpha(color, dark .16 / light .09)` + border `alpha(color, dark .34 / light .22)`
  (muted: .05/.12 + secondary text) (`src/pages/resellers/ResellerIdentity.tsx:9-42`).
- **Count badge:** error-colored pill (radius 999), `px 0.9`, minWidth 20, 12/800,
  lineHeight `"20px"` (`src/portal/storefront/StorefrontShell.tsx:126-143`,
  `src/portal/storefront/TopupsPage.tsx:536-546`); icon badges via MUI `Badge`
  `color="error"` `max={9}` (`src/portal/NotificationsBell.tsx:42`).
- **LiveRate widget:** `50px` pill, `px 1.25 / py 0.5`, `alpha(primary,0.05)` bg,
  divider border (warning border when stale), 16px vertical divider between rates
  (`src/components/LiveRate.tsx:33-59`).

### 3.15 Misc components

- **IconButton:** radius 10, spring hover/active scaling (`src/theme.ts:523-532`);
  standalone 40×40 touch target for the mobile ⋮
  (`src/components/RowActionsMenu.tsx:76-83`).
- **RowActions:** desktop = icon row (`spacing 0.3`, small IconButtons + tooltips);
  mobile = primary actions as small outlined labeled buttons + overflow ⋮ Menu with
  icon+label items, `color`-tinted `ListItemIcon` (`src/components/RowActionsMenu.tsx`).
- **Accordion:** tier‑1 glass, radius `14px !important`, `mb 8px`, no divider
  pseudo-element (`src/theme.ts:511-522`); Help pages override radius sx‑3
  (`src/pages/Help.tsx:259`, `src/portal/pages/Help.tsx:189`) → §5-C15.
- **Tree rows (resellers):** connectors = 1px divider-colored inline-start/bottom
  borders with an 8px inner corner, offset `(depth-1)*24+10`, indent `depth*3.1`;
  toggle = 28px IconButton on `alpha(primary,0.09)`; leaf dot 7px (root `primary.main`,
  else `text.disabled`); root names 800, children 600
  (`src/pages/resellers/ResellerIdentity.tsx:74-160`).
- **Copy/inset rows:** LTR monospace 13 (12.5 portal panels) on `action.hover`,
  radius sx‑2, with copy IconButton (`src/portal/PayDialog.tsx:27-54`,
  `src/portal/pages/Panels.tsx:105-118`).
- **QR block:** white `#fff` box `p: 1` radius sx‑2 (QR must stay on white in dark
  mode), QRCodeSVG size 132 level M (`src/portal/PayDialog.tsx:17-25`); 2FA QR 170px
  with `rgba(120,130,170,0.28)` 8px border (`src/pages/AccountBackup.tsx:261-262`).
- **Logo tiles:** 40px radius sx‑2.5 (sidebars), 40px radius `"10px"` rotate −4°
  (login aside), 56px radius sx‑3 (portal entry/login) — gradient + §2.9 shadows
  → size/radius variants §5-C7.

---

## §4 Patterns

### 4.1 Page anatomy

*(Corrected in v1.1 — the v1.0 text over-generalized the header block.)* Two header
conventions coexist (recorded as §5-C22, kept per app):

- **Admin pages:** the page title lives in the AppBar (from the NAV label,
  `src/components/Layout.tsx:296-298`); most pages start directly with their
  toolbar/filters and add an in-page `variant="h5"` header **only when they carry a
  subtitle or page-level explanation** — 3 of 14 do (`src/pages/Dashboard.tsx:272`,
  `src/pages/resellers/index.tsx:197`, `src/pages/Tools.tsx:594`). Settings replaces
  the header with its sticky glass action bar (`src/pages/Settings.tsx:600-620`).
- **Portal pages:** every page opens with an in-page `variant="h5"` title + optional
  `body2 text.secondary` subtitle at `mb 0.4` (e.g. `src/portal/pages/Panels.tsx:54-57`,
  `src/portal/pages/Payments.tsx:56`), in addition to the AppBar label.

Shared shape when a header exists: `Stack direction {xs column / sm row}
justifyContent space-between`, `mb 2–2.5`, actions/filters on the opposite side. Then:
optional SegmentedTabs row → summary line/chips → one Card containing the
table/content → dialogs at the end. Filters are compact controls: search
`TextField size="small"` with start `SearchIcon` adornment
(`src/pages/resellers/index.tsx:200-236`), `Select size="small"`, `PeriodPicker`.
Desktop table scroll bound: `maxHeight: calc(100vh - 300px)`
(`src/pages/Invoices.tsx:398`) — six variant offsets exist → §5-C20.

### 4.2 RTL rules (hard rules)

1. The app is RTL end-to-end (`index.html:2`, `src/theme.ts:104`, stylis RTL plugin
   `src/rtlCache.ts`). Never hand-flip with physical `left/right` when a logical
   property (`insetInlineStart`, `paddingInline`, `borderInlineStart`, `ps/ms`) exists —
   the codebase is already written this way.
2. **LTR is scoped to the input element only:** `inputProps={{ dir: "ltr" }}` on the
   TextField — never `dir` on the whole field — so label + helperText stay RTL
   (M34 rule; 30+ occurrences, e.g. `src/pages/Setup.tsx:107,114,119,121`,
   `src/pages/Settings.tsx:490`, `src/pages/AccountBackup.tsx:267`).
3. **What is LTR:** tokens, UUIDs, hosts/domains, URLs, wallet addresses, TXIDs,
   invoice/tracking numbers, emails, OTP/captcha codes, dates in Gregorian form,
   version strings, raw error text. Display-side LTR uses `dir="ltr"` on the value
   cell/box (e.g. `src/pages/Invoices.tsx:424`, `src/pages/Payments.tsx:270,274-275`,
   `src/components/CapacityBar.tsx:19`, `src/components/Layout.tsx:236`,
   `src/components/ErrorBoundary.tsx:46`), usually with `fontFamily: "monospace"`.
4. Login is the one screen with a root `dir="ltr"` grid (to pin the illustration side)
   that re-establishes `dir="rtl"` for the form column
   (`src/pages/Login.tsx:123,136,151`).
5. Charts render Persian digits/labels; ECharts tooltips are HTML strings built from
   already-localized values (`src/pages/Dashboard.tsx:170-174`).
6. **Every numeric input is `NumberField`** — see §4.3a. `<TextField type="number">` is
   banned app-wide: inside `dir="rtl"` it mis-places the caret and reverses Backspace.

### 4.3 Dates & numbers

- **Billing periods are Gregorian months** (`"YYYY-MM"`); the PeriodPicker deliberately
  shows ASCII digits + short English month names (`src/components/PeriodPicker.tsx:4-9`).
- **Display dates are Jalali** via `fmtDate` → `toLocaleDateString("fa-IR")`
  (`src/format.ts:15-22`) — used in tables (payments, logs, financial history).
- **Exact moments** (panel sync, notifications) use `fmtDateTime` → Gregorian
  `YYYY-MM-DD HH:mm` in Asia/Tehran, LTR (`src/format.ts:24-38`,
  `src/portal/NotificationsBell.tsx:71`).
- **Numbers:** Persian digits via `toLocaleString("fa-IR")` (`fmtNum`); Toman =
  rounded integer + « تومان» (`fmtToman`); GB ≤2 decimals + « گیگابایت» (`fmtGb`)
  (`src/format.ts:1-13`). Chart axes compress: `م` for millions, `هزار` for thousands
  (`src/pages/Dashboard.tsx:156-159`). Percentages ≤1 decimal
  (`src/pages/Dashboard.tsx:42-44`) with `٪`. Currency display: Toman is primary;
  USDT/crypto amounts render LTR inline `<b dir="ltr">` (`src/pages/Payments.tsx:379-401`)
  and, when formatted, use **ASCII digits via `toLocaleString("en-US")`** (crypto values
  are identifiers-adjacent, never Persian-digit: `src/portal/PayDialog.tsx:173,185,197`).
  (The former Settings raw-ISO-UTC deviation was resolved in DS08 → §5-G8.)
- Identifiers («#N» tracking, 8-digit invoice numbers) stay ASCII in monospace;
  search inputs accept Persian digits (normalized server-side, comments
  `src/pages/Invoices.tsx:77-79`, `src/pages/Payments.tsx:45-47`).

### 4.3a Numeric inputs — `NumberField` (hard rule)

`src/components/NumberField.tsx` is the ONLY numeric input in the app (27 call sites,
migrated 2026-08-18). Never render `<TextField type="number">`; the three reasons are
structural, not cosmetic, and each one was a real complaint:

1. **`type="text"` + `inputMode`, never `type="number"`.** A number input inherits the
   page's `dir="rtl"`, which puts the caret on the wrong side of the digits and makes
   Backspace delete the wrong end; it also reports `""` for a half-typed value and
   changes on scroll-wheel. Digits are constrained by the component's own `sanitize`
   instead — stricter than the browser, and it strips a pasted «۵۰٬۰۰۰ تومان» to `50000`.
2. **Text in, text out** (`value: string`, `onChange: (raw: string) => void`). Holding a
   `number` and doing `Number(event.target.value)` turns an emptied field back into `0`
   on the next render, so the field cannot be cleared at all. Parse ONCE, at submit,
   with `numberValue()` — it returns `null` for empty/unparseable, never `0`. Range
   clamping belongs on blur or submit, never per keystroke.
3. **A click selects the whole value.** Tapping a field that already holds a number means
   "replace this"; the second click still places a caret for a one-digit fix.

Persian/Arabic-Indic digits are accepted everywhere and normalized to ASCII — the
Telegram bot has always done this (`app/bot/storefront/handlers.py` `_digits`), and the
panel now matches. `dir: "ltr"` is applied to the input element only (§4.2 rule 2), so
the Persian label and helper text stay RTL. Behaviour is pinned by
`src/test/number-field.test.tsx`.

### 4.4 Confirmation & mutation conventions

- **`window.confirm`** with a specific Persian question for destructive/irreversible
  one-click actions (delete panel/payment, enforce, discard drafts, bulk send, restart,
  update; census: `src/pages/Panels.tsx:141,204`, `src/pages/Payments.tsx:140,145`,
  `src/pages/Invoices.tsx:237-307`, `src/pages/Broadcast.tsx:269,324,436`,
  `src/pages/AccountBackup.tsx:382,414`,
  `src/pages/resellers/ResellerActions.tsx:30`,
  `src/portal/storefront/StorefrontManagersPage.tsx:107`,
  `src/portal/storefront/StorefrontSettingsPage.tsx:206,231`,
  `src/portal/storefront/StorefrontPlansPage.tsx:186`). Questions state scope and
  irreversibility («… قابلِ بازگشت نیست», «برای همیشه حذف شود؟»).
- **Dialog** when input or review is needed (edit, defer, payment confirm with on-chain
  check, §3.5).
- Mutations run through `useToastMutation` → success/error toast + query invalidation
  (`src/hooks/useToastMutation.ts`); money mutations invalidate `MONEY_KEYS`
  (`src/queryKeys.ts`). Buttons disable while `isPending`.
- Data freshness: react-query `staleTime 60s`, `keepPreviousData`, no refetch on focus,
  retry 1 (`src/api/queryClient.ts:12-21`); adjacent-page prefetch on paged tables
  (`src/pages/Invoices.tsx:105-114`).

### 4.5 Responsive strategy

- One breakpoint story: **md** (900) switches sidebar ↔ drawer+BottomNav and tables ↔
  card lists; **sm** (600) switches dialogs to fullscreen and stacks header rows;
  `resp-table` CSS kicks in below 600.
- Touch affordances: 40px ⋮ target, labeled primary buttons on mobile rows, no
  hover-only information on touch (menu items carry icon+label).
- Density: tables `size="small"`; content padding drops to 16px on xs; StatCard values
  shrink (`{1.35rem/1.65rem}`).
- PWA: standalone display, safe-area insets, SW precaches the app shell + font, API
  never cached (`vite.config.ts:16-66`).

### 4.6 Chart conventions

Native ECharts only through `src/components/EChart.tsx` (never a wrapper lib — the
`echarts-for-react` default-import broke the production bundle; see M58 note and the
tree-shaken module list `src/components/EChart.tsx:8-14`). Rules:

- Colors: accent-gradient bars (§2.6); categorical order `RANK_COLORS` (§2.5); donut
  slices use the status hexes (§2.5, conflict-flagged §5-C8).
- Typography: Vazirmatn, axis 11px (9 compact), secondary text color.
- Tooltips: axis-trigger with shadow pointer for bars, item-trigger for donuts;
  HTML body `date<br/><b>amount</b>`; glassy background per §2.6.
- Geometry: rounded top corners `[6,6,0,0]`, `containLabel: true`, tight grid
  (`left 4-6 / right 10-14 / top 10-18 / bottom 2-4`).
- Every chart: `aria-label` (Persian), height 190–300, memoized option objects
  (`src/pages/Dashboard.tsx:162-164`).
- Hand-rolled HTML meters (dashboard panel/rank bars) follow the same look: 8–9px
  track (radius 99) on `alpha(text.secondary, 0.11)`, RTL fill anchored
  `justifyContent: flex-end`, gradient fill (`src/pages/Dashboard.tsx:390-411,553-570`).

### 4.7 Icon usage

`@mui/icons-material` with **deep ESM imports**
(`import X from "@mui/icons-material/esm/X"` — every file, e.g.
`src/components/Layout.tsx:9-27`). Sizes: default 24 in nav/menus, `fontSize="small"`
in table actions, 17 for inline text-adjacent icons, 19/23 glyphs inside tiles,
36/48 for hero states. Icons never appear without an accessible name (tooltip,
`aria-label`, or adjacent label).

### 4.8 Copy conventions that are design rules

Persian UI copy throughout; ZWNJ (نیم‌فاصله) used in compounds; Latin terms
(USDT/TXID/Face ID) appear inline untranslated; empty states are sentences ending in
period; toasts are short sentences («ذخیره شد», «خطا در …»). Currency unit always
follows the number («تومان»), tracked identifiers prefixed «#».

---

## §5 Conflict log & gaps

*(Program complete — every entry below is resolved, closed-by-design, or deferred
by owner decision; the DS09 lint enforces the end state.)*

Canonical-selection rules, in order: **(a)** `theme.ts` wins over ad-hoc styling;
**(b)** otherwise the most frequently used value; **(c)** otherwise the most recent
intent. Every entry below lists variants → canonical → rationale. This section is the
Phase‑2 work-list.

### 5.1 Conflicts

- **C1 — Violet remnants vs Apple-blue system.** **RESOLVED `v1.100.7` (DS05).** Both sidebars consume the shared `SIDEBAR_AMBIENT` blue token
  (`src/themeTokens.ts`, test-pinned to contain no `139,92,246`); the storefront
  StatCard accent `#7c5cff` became `#0ea5e9` (the sibling portal-dashboard set's
  established 4th accent). `#8b5cf6` as the *Tools nav categorical color* (§2.4)
  remains — categorical ≠ ambient, never a conflict. Original variants: admin violet
  ambient `rgba(139,92,246,.28|.20)` vs the portal blue.
- **C2 — Legacy navy palette remnants (pre-Apple theme).** **RESOLVED `v1.100.6`
  (DS03 removed the Settings-header bg; DS04 swept everything else).** The
  navy-kill re-grep (`14,16,32 | 11,13,25 | 9,11,20 | 22,26,43 | 30,40,100 |
  31,38,80 | 35,69,108 | 120,130,170 | 200,210,255 | #e2e8f0 | #334155`) returns **0**
  in `src/`. Applied targets: chart tooltips → the shared `chartTooltip(theme)`
  helper (glass Tooltip surface + `text.primary`, `src/components/chartTooltip.ts`);
  login dark card → `rgba(28,28,30,0.55)`; both desktop sidebars → the chrome tokens
  (`CHROME_SIDEBAR_BG.dark = rgba(28,28,30,.50)`); light shadows/ring/drop-shadow →
  neutral black at the same alphas (`0,0,0,.18/.26/.22/.14` + ring `0,0,0,0.08`);
  QR border → `rgba(0,0,0,0.12)`. Original variants and their sites are preserved in
  the v1.1 history of this entry (git).
- **C3 — Two mobile table→card mechanisms.**
  Variants: global `resp-table` CSS + auto labels (`src/theme.ts:176-204`,
  `src/responsive.ts:19-51`) vs explicit `isMobile` card branches with `nestedCardBg`
  (`src/pages/Invoices.tsx:439-482`, `src/pages/Payments.tsx:286-335`,
  `src/pages/resellers/ResellerMobileCard.tsx`). Invoices/Payments even carry
  `className="resp-table"` on tables that never render on mobile (the branch
  switches first).
  **Canonical:** explicit cards (pattern A) for action-heavy list pages; `resp-table`
  (pattern B) for simple/read-only or in-dialog tables. Rationale: (b) every major
  list page that was redesigned uses pattern A; (c) pattern A is the newer
  "resellers pattern" (comment `src/pages/Invoices.tsx:440`). *(DS06: the two
  redundant classes on the pattern-A Invoices/Payments desktop tables were dropped —
  entry RESOLVED.)*
- **C4 — Login page deviates from the control system.**
  Variants: fields height 58 / radius `"14px"` / placeholder-only / explicit RTL
  inputProps (`src/pages/Login.tsx:27-41`) vs theme pill inputs with labels
  (`src/theme.ts:303-334`); submit radius `"14px"` height 54 / passkey `"12px"`
  height 46 (`src/pages/Login.tsx:336-343,360`) vs pill buttons 980
  (`src/theme.ts:341`).
  **Canonical:** the theme pill system app-wide; the Login screen is recorded as a
  **deliberate hero-screen exception** (bigger targets, marketing-style card). Phase 2
  must treat Login's values as an allowed local override, not spread them further.
  Rationale: (a) theme wins generally; the deviation is confined to one pre-auth
  screen and visually intentional (M58 redesign).
- **C5 — Nine blur/saturate recipes for one material.** **RESOLVED (DS01 `v1.100.5`
  + DS03 `v1.100.6`).** The canonical trio — tier‑1 mode split, tier‑2
  `blur(40px) saturate(180%)`, chrome `blur(48px) saturate(220%) brightness(1.03)` —
  is now the only set in `src/` (recipe census: 3 distinct recipes; drift re-grep 0),
  exported as `TIER1_BLUR`/`TIER2_BLUR`/`CHROME_BLUR` in `src/themeTokens.ts` and
  test-pinned. DS03 converged: nav-selected 16/180, SegmentedTabs 12/180, Settings
  papers 28/200/1.02 (→ tier‑1) + header 14/180 (→ tier‑2, incl. its bg → `TIER2_BG`,
  which also removed the C2 navy `rgba(22,26,43,.62)` per the DS03/DS04 coordination
  note), login hero 1.04 (→ chrome) and captcha 16/180 + 12/160 (→ tier‑2).
  Original variants: 48/220/1.03, 48/220/1.04, 40/180, 28/200/1.02, 20/180, 20/140,
  16/180, 12/160-180, 14/180.
- **C6 — Four spellings of "pill".** **RESOLVED `v1.100.7` (DS07):** two spellings remain —
  `980` for controls/badges/meters (the `99`/`999` sites were normalized:
  StatusPill, four Dashboard meter tracks/fills, StorefrontShell + Topups badges)
  and `"50px"` for input-height pills. Rendering identical (all pills exceeded
  half-box height).
- **C7 — "Icon tile" has 6 ad-hoc size/radius variants.**
  Variants: 31px/sx‑2 (nav chips, `Layout.tsx:180-182`), 30px/sx‑2 (rank,
  `Dashboard.tsx:531-534`), 36px/sx‑2 (portal panels, `portal/pages/Panels.tsx:95`),
  40px/sx‑2.5 (sidebar logos, `Layout.tsx:137-140`), 40px/`"10px"` (login logo,
  `Login.tsx:399-405`), 44px/`"14px"` (StatCard, `StatCard.tsx:49-52`),
  56px/sx‑3 (portal entry, `PortalEntry.tsx:134`).
  **Canonical:** three sizes — 31/sx‑2 (inline/nav), 44/`"14px"` (card-level,
  StatCard's), 56/sx‑3 (hero) — chosen by (b) frequency within each role; 40px logo
  tiles stay as the brand-block size (b). The `"10px"` login radius folds into C4's
  exception. **RESOLVED `v1.100.7` (DS07):** the two stragglers converged to 31px (Dashboard
  rank tiles 30→31, portal Panels tile 36→31).
- **C8 — Invoice-status colors encoded twice.** **RESOLVED `v1.100.6` (DS02).**
  Donut fills now come from `invoiceStatusColor(palette, status)` and StatusPills from
  `statusPillColors(palette)` (both in `src/themeTokens.ts`, test-guarded) — one
  semantic set, the §2.1 palette. The contrast caveat was honored by using the
  MUI-computed `success.dark`/`warning.dark` for light-mode 12px pill text (mechanism-
  canonical, no invented hex). Original finding: three different greens
  (`#28cd41|#30d158` palette vs `#34d399` donut vs `#10b981` pill) all meant "good";
  the donut/pill hexes shadowed the palette.
- **C9 — `src/pages/Sales.tsx:14` STATUS_COLOR lacks `canceled`.** **RESOLVED
  `v1.100.6` (DS02):** the map now declares the full 6 states
  (`canceled: "default"`), matching `src/pages/Invoices.tsx:44`.
- **C10 — Admin vs portal chrome drift.** **RESOLVED `v1.100.7` (DS05).**
  Both layouts share `SIDEBAR_WIDTH = 256`; the portal `navItemSx` is byte-identical
  to admin's (verified by diff in the batch gate) and the icon chips carry the same
  gloss/spring/rim treatment (source shapes differ — admin `.join()` array vs portal
  template literal — but the rendered CSS is identical). The version-footer clause is
  closed as **N/A**: the portal API exposes no version endpoint and backend additions
  are outside this program. Original finding: width 256 vs 248 + missing selected
  backdrop-filter/gloss/hover-scale on portal.
- **C11 — Two brand-gradient end colors.** In-app tiles end at `#0071e3`
  (`Layout.tsx:144` etc.); the favicon/manifest icon ends at `#0064d2`
  (`public/favicon.svg`). **Canonical:** `#5ab5ff → #0071e3` in-app; the icon's darker
  stop is accepted as an asset-specific contrast tweak (recorded, not to be copied
  into the app).
- **C12 — Progress-bar geometry variants.** 6/4 (theme `src/theme.ts:547`), 6/3
  (CapacityBar), 7/3 (portal Subs), 8/4 (storefront), 8-9/99 (dashboard meters), and
  *(added v1.1)* default-height/radius sx‑1 = 14px
  (`src/portal/storefront/StorefrontCampaignsPage.tsx:175`).
  **Canonical:** theme 6/radius 4 for LinearProgress-based bars (rule a); the
  hand-rolled dashboard meters keep 8–9px tracks as a distinct "meter" component (b),
  now radius-980-spelled per C6. **RESOLVED `v1.100.7` (DS07):** all four LinearProgress
  overrides deleted (CapacityBar, portal Subs, storefront plan bars, campaigns
  progress) — the theme provides 6px/4px; guarded by
  `src/test/capacity-bar.test.tsx`.
- **C13 — `SectionCard` and `EmptyState` duplicated** in
  `src/pages/Dashboard.tsx:65-114` and `src/portal/ui.tsx:4-31` (near-identical).
  **Canonical:** `src/portal/ui.tsx` as the shared home (it is already imported
  portal-wide). **RESOLVED `v1.100.7` (DS07):** the admin Dashboard's local copies were deleted
  and it imports `SectionCard`/`EmptyState` from `../portal/ui`.
- **C14 — Empty-state min-heights differ:** 210 (Dashboard) vs 180 (portal ui).
  **Canonical:** 180. **RESOLVED `v1.100.7` (DS07)** via C13 — Dashboard now uses the shared
  `EmptyState` (180). Table-cell empties (`py 4/5`) remain the sanctioned compact
  form.
- **C15 — Accordion radius:** theme `14px !important` (`src/theme.ts:516`) vs Help
  pages sx‑3 = 42px (`src/pages/Help.tsx:259`, `src/portal/pages/Help.tsx:189`) — the
  theme's `!important` actually wins at runtime, so the sx was dead styling.
  **Canonical:** 14px (rule a). **RESOLVED `v1.100.7` (DS07):** both dead overrides deleted —
  zero rendered change.
- **C16 — Drawer paper vs desktop sidebar glass.** **RESOLVED `v1.100.5` (DS01).**
  The theme's MuiDrawer paper now uses the chrome recipe with the neutral dark tint
  via `src/themeTokens.ts` (`CHROME_BLUR`, `CHROME_SIDEBAR_BG`,
  `CHROME_SIDEBAR_BORDER`), guarded by `src/test/theme-contract.test.ts`. The desktop
  sidebars' own dark tint (`rgba(9,11,20,.50)`, still navy) converges in DS04/C2.
  Original finding: mobile drawer rendered `rgba(28,28,30,0.88)` + blur 40/180 while
  desktop rendered the chrome recipe — the same sidebar, two looks.
- **C17 — AppBar double styling.** Theme paints the AppBar glass
  (`src/theme.ts:270-283`) while Layout also passes `color="transparent"` +
  `elevation={0}` (`Layout.tsx:288`, `src/portal/PortalLayout.tsx:219`).
  **Canonical (reclassified v1.1): keep both props — they are load-bearing, not
  redundant.** `color="transparent"` guarantees MUI's color-variant class
  (`colorPrimary` background) never competes with the theme's root glass background,
  and `elevation={0}` suppresses the default elevation-4 shadow. Resolution: no code
  change; the v1.0 "Phase 3 cleanup" note is withdrawn.
- **C18 — BottomNav translucency (dark `rgba(0,0,0,0.72)`, light
  `rgba(255,255,255,0.86)`) near-duplicates AppBar (0.60 / 0.80)**
  (`src/components/BottomNav.tsx:40`, `src/theme.ts:275`). **Canonical:** keep both —
  the deeper bottom tint is deliberate over scrolling content — but they are recorded
  as separate tokens (`chrome.appBar`, `chrome.bottomNav`) so neither drifts further.
- **C19 — Skeleton radii:** DataState rows radius 1.5 (=21px,
  `src/components/DataState.tsx:40-42`) vs Dashboard skeletons `"18px"`
  (`src/pages/Dashboard.tsx:292-305`). **Canonical:** match the shape being faked —
  `"18px"` when faking Cards, default otherwise; DataState's 1.5 approximates inner
  rows and stays.

*Entries C20–C24 were added in v1.1 by the Phase‑2 audit (new deviations not caught in
Phase 1):*

- **C20 — Table scroll-bound variants** *(added v1.1)*. **RESOLVED `v1.100.7` (DS06, per
  confirmed D14):** all eight desktop tables consume `TABLE_SCROLL_BOUND =
  "calc(100vh - 300px)"` from `src/themeTokens.ts` (test-pinned); responsive
  `{ xs: "none", sm|md: … }` forms kept; `src/pages/Payments.tsx` also switched
  from `height:` to `maxHeight:` (the −120px max-height change was acknowledged in
  the sign-off). Original variants: 300 (Invoices), 320 (ResellerTable), 260 ×3
  (FinancialHistory, Logs ×2), 240 (Sales), 220 (Debts), 180-via-`height:`
  (Payments).
- **C21 — Dialogs missing the xs-fullscreen convention** *(added v1.1)*. §3.5's
  `fullScreen={useXsFullScreen()}` convention holds in the admin money flows
  (Invoices ×3, Payments, EditReseller, BumpLimits, PayDialog) but **21 dialog roots
  omit it**: `src/pages/Tools.tsx:124,242,370,570` (570 also lacks
  `fullWidth maxWidth`), `src/pages/Panels.tsx:215`,
  `src/pages/resellers/AbsentResellers.tsx:117`,
  `src/portal/pages/Subs.tsx:273,290`, `src/portal/pages/Panels.tsx:132`,
  `src/portal/storefront/StorefrontConflictDialog.tsx:19`,
  `src/portal/storefront/StorefrontPlansPage.tsx:197`,
  `src/portal/storefront/TopupsPage.tsx:370,408,440,662`,
  `src/portal/storefront/StorefrontPlanHistoryDialog.tsx:24`,
  `src/portal/storefront/StorefrontCampaignsPage.tsx:207`,
  `src/portal/storefront/CustomerDetailPage.tsx:428,802`,
  `src/portal/storefront/StorefrontCreditsPage.tsx:249,336`.
  **RESOLVED `v1.100.7` (DS06):** all 21 roots now carry `fullScreen={useXsFullScreen()}`
  (18 containing components gained the hook; Tools' confirm dialog also gained
  `fullWidth maxWidth="xs"`). Gate re-grep: zero `<Dialog …>` roots without
  `fullScreen` remain in `src`.
- **C22 — Page-title anatomy split (admin vs portal)** *(added v1.1)*. 10 of 14 admin
  pages have **no in-page title** (the AppBar label is the title); every portal page
  renders an in-page `h5` **in addition to** the AppBar label (census in §4.1).
  **Canonical:** keep per-app conventions as corrected in §4.1 (admin: AppBar-title,
  in-page h5 only with subtitle; portal: in-page h5 standard). No code change —
  recorded so Phase 3 doesn't "unify" it blindly; revisit only via a doc-first change.
- **C23 — Tables with no mobile adaptation** *(added v1.1)*. Eight simple tables use
  neither mobile pattern A nor B — they only scroll horizontally:
  `src/pages/Tools.tsx:74-75,206-207,331-332,509-510` (wrapped in
  `Box overflowX:"auto"`), `src/portal/pages/Invoices.tsx:53-54` and
  `src/portal/pages/Payments.tsx:65-66` (wrapped in `Card overflowX:"auto"`),
  `src/pages/Broadcast.tsx:240-241,388-389` (bounded preview boxes, `maxHeight
  320/360`). **RESOLVED `v1.100.7` (DS06):** all eight tables carry `className="resp-table"`,
  and `src/portal/PortalLayout.tsx` now mounts `useResponsiveTableLabels()` (the
  functional prerequisite — labels were previously admin-only). The Broadcast
  preview boxes were included; if the <600px visual check shows them degraded inside
  their bounded scroll boxes, dropping those two classes is the sanctioned rollback.
- **C24 — LTR display via sx `direction` instead of the `dir` attribute** *(added
  v1.1)*. `src/pages/Broadcast.tsx:313,315` set
  `sx={{ direction: "ltr", textAlign: "left", … }}` on link-preview `code` boxes. All
  emotion-generated CSS passes through the stylis RTL plugin (cssjanus semantics,
  `src/rtlCache.ts:5-8`), which **flips physical values** — `textAlign: "left"` and
  `direction: "ltr"` compile to their RTL counterparts, the opposite of the intent.
  Every other LTR display site uses the `dir="ltr"` **attribute**, which bypasses the
  CSS transform (§4.2 rule 3). **Canonical:** the `dir="ltr"` attribute (+ drop the
  `textAlign`/`direction` sx keys). Rationale: rule (b) — the attribute is the
  app-wide mechanism — and mechanism correctness under the RTL pipeline.
  **RESOLVED `v1.100.7` (DS07):** both boxes now use `dir="ltr"`; the link previews render
  truly LTR for the first time.
- **C25 — `dir` on a TextField root** *(added during DS08)*.
  `src/pages/Panels.tsx` panel-link paste field passes `dir="ltr"` on the TextField
  itself (multi-line JSX — the Phase‑2 single-line grep missed it), which flips its
  label/helperText too, violating the §4.2 hard rule (`inputProps={{ dir: "ltr" }}`
  only — the M34 lesson). **Canonical:** move to `inputProps`. **RESOLVED `v1.100.7` (DS09):**
  the field now uses `inputProps={{ dir: "ltr" }}`; its label/helperText render RTL
  again.

### 5.2 Gaps (real absences — recorded, not invented)

- **G1 — No token module.** **RESOLVED `v1.100.5`→`v1.100.7` (DS01→DS08, per amended D1).**
  `src/themeTokens.ts` now holds every shared §2 constant: glass tiers + chrome
  recipe (DS01/03/04), status-color mappings (DS02), sidebar ambient/width + nav
  glosses (DS05), table scroll bound (DS06), and — completing the sweep in DS08 —
  `EASE_ENTRANCE`/`ENTRANCE_BEZIER`, `SPRING_BEZIER`, `CHART_FONT`,
  `GLOSS_STATCARD_TILE`, all consumed byte-identically (theme.ts transitions/
  keyframe strings are token-composed; the framer EASE duplicate in Dashboard is
  gone). Everything is pinned by `src/test/theme-contract.test.ts`.
- **G2 — No authored disabled/error-input states.** **CLOSED doc-only (D2, DS09 `v1.100.7`):** MUI defaults ARE the canonical disabled/error states (recorded in §3.2);
  only `error={…}` flags are set (e.g. `src/pages/Setup.tsx:111`).
- **G3 — Portal has no BottomNav** on phones (admin does). **DEFERRED (D3,
  2026-07-23):** feature work, not conformance; the portal drawer suffices. Stays in
  the plan's §G future-ideas appendix.
- **G4 — No shared ECharts theme.** **RESOLVED `v1.100.6` (DS04, per approved D4):**
  `src/components/chartTooltip.ts` now provides the shared tooltip surface consumed
  by all three chart builders; axis/font declarations remain per-builder by design
  (they already read the theme directly).
- **G5 — No z-index scale.** **CLOSED doc-only (D5, DS09 `v1.100.7`):** MUI defaults + the
  documented −1/0/1/2/3 layers (§2.12) are the standard; the lint enforces the set.
- **G6 — No written type ramp.** **CLOSED doc-only (D6, DS09 `v1.100.7`):** §2.7's ladder and
  size vocabulary ARE the written standard, and the lint enforces both sets.
- **G7 — Palette duplications** (`info` ≡ `primary`; light `secondary` ≡ light
  `warning` `#ff9500`). **CLOSED doc-only (D7, DS09 `v1.100.7`):** kept and documented as
  intentional Apple-palette facts.
- **G8 — Third datetime format.** **RESOLVED `v1.100.7` (DS08, per approved D8):** the
  Settings rate timestamp now renders via the existing `fmtDateTime` (Tehran,
  §4.3). Safe because the backend writes timezone-aware UTC isoformat
  (`backend/app/services/rates.py` uses `datetime.now(timezone.utc).isoformat()`),
  which `new Date()` parses correctly.
- **G9 — `frontend/placeholder.html`.** **RESOLVED `v1.100.7` (DS08, per approved D9):**
  deleted, along with its dead `.dockerignore` entry. (Was a pre-M7 dev artifact
  with retired slate colors, never served by production builds.)
- **G10 — Focus-visible on custom clickables.** **RESOLVED `v1.100.7` (DS08, per approved
  D10):** the two copy rows (`src/portal/PayDialog.tsx` CopyRow,
  `src/portal/pages/Panels.tsx` link box) carry `tabIndex={0}` + `role="group"`, so
  the global `:focus-visible` ring reaches them; the inner copy buttons were already
  focusable.
- **G11 — No empty-state illustration standard.** **CLOSED doc-only (D11, DS09 `v1.100.7`):**
  text-only IS the standard (§3.14).
- **G12 — `src/pages/Debts.tsx` connection chip.** **RESOLVED `v1.100.6` (DS02, per
  approved D12):** «بدون ربات» is now `color="default"` + `variant="outlined"`,
  matching the muted-pill severity for the same state. Originally it used `error`
  while the resellers pages used a muted grey StatusPill.
- **G13 — Loading-state coverage is incomplete** *(added v1.1)*. **RESOLVED `v1.100.7` (DS08,
  per approved D13):** the three search Tools tables wrap in
  `DataState isLoading/isError/onRetry` (skeleton per search), the recover tool and
  both Broadcast result tables show a `DataState` skeleton while their mutations are
  pending, and the Panels table card wraps in `DataState` with its query flags.

### 5.3 Top 5 worst inconsistencies (Phase‑2 preview)

1. **C2** — legacy navy surfaces/shadows in five high-visibility places (charts'
   tooltips, Login, both sidebars, Settings header).
2. **C8** (+C9, G12) — three different "success/status" color sets for the same
   invoice/connection semantics.
3. **C5** — nine glass recipes where three tiers are intended.
4. **C1** — the admin sidebar still glows violet while the system (and the portal
   twin) is Apple blue.
5. **C10/C16** — the same navigation chrome renders differently between admin vs
   portal and desktop vs mobile.

---

## §6 Governance

1. **This document + `docs/design-tokens.json` are the single source of truth for UI
   decisions.** Any future visual change updates this document (and the JSON if a §2
   token changes) **first**, then the code. A PR that changes a visual value without a
   matching doc update is incomplete.
2. Additions follow the same bar as this extraction: cite `file:line`, name the token
   role, and either match an existing token or add a §5 entry consciously.
3. The §5 conflict log is the authoritative Phase‑2 audit input and the Phase‑3
   standardization work-list; resolving an entry means updating both code and this doc
   (move the entry to a "resolved" note with the release number).
3b. **Enforcement:** `frontend/scripts/design-lint.mjs` (`npm run lint:design`, run in
   the CI frontend job) is the §5 enforcement mechanism. Its color allowlist is
   harvested from `docs/design-tokens.json` at lint time; its structured vocabularies
   (radii, sizes, weights, z-index, blur trio, scroll bound) mirror §2. Extending any
   allowlist requires a doc-first token addition here + in the JSON — a code-only
   change cannot pass CI.
4. Doc version: **2.0 "standardized"**, 2026-07-23 (DS09). v1.1 was the Phase‑2
   audit errata; v1.0 extracted 2026-07-22 against `v1.100.4`
   (frontend at commit `78179b6`); v1.1 is the Phase‑2 audit errata — §4.1 corrected
   (page-title anatomy), §4.3 crypto-digit rule added, §5 extended with C20–C24 and the
   C12 sx‑1 variant, and `design-tokens.json` extended with 14 entries (E1–E14) that §2
   documented but the v1.0 mirror omitted. The code itself is unchanged since `d5d8c9f`.
   Re-verify line citations after any large frontend refactor; the tokens themselves
   are anchored by value + role, not line numbers.
