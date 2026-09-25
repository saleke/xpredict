# LISA Dashboard — Design System Inspection

Source of truth for the LISA web dashboard visual language. Written for another AI agent to build/refine UI quickly. Applies to `web/index.html`, `web/css/index.css`, `web/css/auth.css`, and the class-driven markup emitted by `web/js/app.js`.

Reference family: Bloomberg/Linear "institutional precision" crossed with a premium consumer sportsbook (Pinnacle dark, Sofascore density discipline). Dark-only, data-dense, numeric-first.

---

## 1. Visual Theme & Atmosphere

Dark obsidian canvas with layered **tone-on-tone** surfaces (not glassmorphism). Calm, precise, "money-desk" feel. Every number renders in a tabular monospace face; every field carries an uppercase micro-label. One confident brand accent (cyan) drives interactivity; status colors (emerald / amber / rose) are reserved exclusively for meaning and are **always paired with a word or icon**, never used alone.

The layout communicates hierarchy through **grouping and whitespace** (cards, section rhythm) instead of borders and boxes. Data tables are the exception — they keep hairlines because they are reference material, not the primary read.

---

## 2. Color Palette & Roles

### Surfaces
- **`--surface-canvas`** (`#05070C`): page background. Deep, near-black navy.
- **`--surface-body`** (`#0B1018`): main content band behind cards.
- **`--surface-card`** (`#0E141F`): default card surface.
- **`--surface-card-2`** (`#131B29`): nested / elevated surface inside a card (metric cells, code pills).
- **`--surface-hover`** (`#172031`): interactive card hover.
- **`--surface-overlay`** (`rgba(5,7,12,0.86)`): modal backdrops.

### Borders
- **`--border-hair`** (`rgba(148,163,184,0.12)`): default 1px card/hairline separators (slate-based, not white — softer on the eye).
- **`--border-strong`** (`rgba(148,163,184,0.22)`): controls, active states, table thead bottom.
- **`--border-brand`** (`rgba(56,189,248,0.45)`): active accent stroke.

### Brand (Electric Neon Lime)
- **`--brand-300`** `#d9ff33` (bright text on dark, links hover)
- **`--brand-400`** `#ccff00` (primary text accent, active underline, electric lime)
- **`--brand-500`** `#b8e600` (primary CTA fill, active stroke, glow source)
- **`--brand-ink`** `#070a08` (text on brand-filled buttons)

### Semantic
- **`--pos`** `#34D399` — win, positive EV, low uncertainty, high confidence, "LOW".
- **`--pos-soft`** `rgba(52,211,153,0.12)` — soft fill behind `--pos` text.
- **`--warn`** `#f97316` — warm amber, medium uncertainty, parlay, VIP/Tier 3 reference, marquee "PICK OF THE DAY".
- **`--warn-soft`** `rgba(249,115,22,0.12)`.
- **`--neg`** `#FB7185` — loss, negative EV, high uncertainty, "do not stake".
- **`--neg-soft`** `rgba(251,113,133,0.12)`.
- **`--violet`** `#A78BFA` — derived/analytic surfaces (pivots, smart-parlays, portfolio), draw in the H/D/A bar.
- **`--info`** `#ccff00` alias of brand-400 (used for neutral-informative chips).

### Text scale
- **`--text-1`** `#EEF2F8` — primary
- **`--text-2`** `#9AA8BC` — secondary
- **`--text-3`** `#5C6B82` — muted (timestamps, captions)
- **`--text-inverse`** `#070a08`

### Tier identity (one accent per tier — kept consistent everywhere: pricing, matrix, pills, user tag)
- free `slate` (`#9AA8BC`), tier1 `emerald`, tier2 `neon-lime`, tier3 `gold`.

---

## 3. Typography Rules

### Font Family
- Primary: `'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`
- Monospace (ALL numbers, codes, scores, time): `'JetBrains Mono', 'SF Mono', 'Menlo', monospace`

### Hierarchy

| Role | Font | Size | Weight | Line Height | Letter Spacing | Notes |
|---|---|---|---|---|---|---|
| Display / hero title | Inter | 34px | 800 | 1.1 | -0.03em | single bold statement |
| Page (view) title | Inter | 24px | 800 | 1.2 | -0.02em | every view opens with one |
| Section heading | Inter | 18px | 700 | 1.3 | -0.01em | |
| Card title | Inter | 15px | 700 | 1.35 | 0 | team names, prices, blockheads |
| Body | Inter | 13.5px | 400 | 1.55 | 0 | dense data product baseline |
| Micro label | Inter | 11px | 600 | 1.2 | +0.07em | uppercase, field captions |
| Mono value (small) | JetBrains Mono | 12.5px | 500 | 1.35 | 0 | table numerics |
| Mono value (large) | JetBrains Mono | 26px | 700 | 1.1 | -0.01em | KPI headlines |
| Meter/badge | Inter | 10.5px | 700 | 1 | +0.05em | uppercase pill text |

Rule: any literal number is mono + `font-variant-numeric: tabular-nums`. Numerics in a table are right-aligned and share a column baseline.

---

## 4. Component Stylings

### Buttons
- **Primary (`--btn-primary`)**: fill `--brand-500`, text `#fff`, radius 10px, height 40px, padding 0 18px, weight 600, hover fill `--brand-400`, active `translateY(1px)`, focus ring 2px `--border-brand` offset 2.
- **Ghost/secondary**: transparent, 1px `--border-strong`, text `--text-1`, hover `--surface-hover`.
- **Tier-upgrade glow (`--btn-upgrade`)**: gradient gold-amber, text `#1A1200`, weight 700, subtle inner shine; reserved for CTA moments only.
- **Pill/segment buttons**: 1px `--border-hair`, radius pill, 11px caps; `.active` = brand-tinted fill + brand border + brand text.
- Micro-actions (book selection chips, copy): 40px squarish chips with inline SVG logos.

### Cards & Containers
- Default: fill `--surface-card`, 1px `--border-hair`, radius 14px, padding 20px. Hover: `--surface-hover` + border `--border-strong`. No floating shadows on canvas (elevation = tone), the one exception is the sticky header and modals which get a soft drop shadow.
- Featured card: 1px brand/cyan faint outline + inner top highlight line (1px gradient at top edge).

### Inputs & Forms
- Fill `--surface-body`, 1px `--border-strong`, radius 10px, height 42px, padding 0 14px, text `--text-1`, placeholder `--text-3`. Focus: border `--border-brand` + 3px translucent cyan ring. Labels: 11px caps micro-label above, `--text-2`.

### Navigation
- Sticky header (30px pad, translucent `rgba(5,7,12,0.72)` + blur 18px). Brand block left, primary nav center (text buttons with 2px bottom indicator, active = brand text + `--brand-400` underline), auth cluster right.
- Secondary tab bar: horizontal segmented rail, scrollable on small screens, 40px pills.
- On mobile the header wraps; nav becomes the pill rail.

### Badges / Tags
- Pill tags: 10.5px caps, padding 3px 10px, radius pill, 1px border tinted by role with a matching translupercolor alpha `*soft` fill.
- Uncertainty badges: medium = amber/▲, high = rose/⚠, low = emerald/●. Never icon-less.
- Result badges: WIN (emerald, `✔ WON`), LOSS (rose `✘ LOST`), VOID (slate `∅ VOID`).

### Links / Interactive States
- Links: brand-400, weight 500; underline on hover. Focus visible ring brand. `cursor: pointer` only where clickable; disabled buttons `opacity .45; cursor not-allowed`.

---

## 5. Layout Principles

### Page Composition
All views share the same shell: `header` → optional `ticker` → `main.container` → each `section.view-panel` with a `.view-head` (title, subtitle, meta chip) opening. Single-column reads, wide content rail (max 1240px), 2-col grids only for complementary panels (picks grid, forecast cards, alpha columns).

Section order in Overview: hero (value prop + CTA) → hero stats → interactive visualizer → forecast teaser → story pillars → proof/ledger → pricing + matrix → CTA banner.

### Spacing System
8px base scale: `--sp-1 4, --sp-2 8, --sp-3 12, --sp-4 16, --sp-5 20, --sp-6 24, --sp-8 32, --sp-10 40, --sp-12 48, --sp-16 64`. Section rhythm 56px; card internal 20px; grid gaps 16px.

### Grid & Container
- `.container`: max-width 1240px, horizontal padding 24 (mobile 16).
- KPI row: `repeat(auto-fit, minmax(190px, 1fr))`.
- Picks grid: `grid-template-columns: repeat(2, minmax(0,1fr))` (≥1080px), 1 col below. Locked cards render at same height via min-height to keep the masonry calm.
- Forecast cards: 2-col grid (≥1120px), 1-col below.
- Tables: full-bleed inside `.table-card`; thead sticky within `.table-responsive` when it exceeds a scroll height.

### Section Rhythm
Every major block is introduced by a `.section-head` = micro-label tag (brand) + heading + subtext. Blocks are separated by whitespace, not rules.

### Border Radius Scale
`--r-sm 8, --r-md 10, --r-lg 14, --r-xl 20, --r-pill 9999px`. Tables keep the card's radius via an overflow wrapper.

---

## 6. Depth & Elevation

| Level | Treatment | Use |
|---|---|---|
| 0 | canvas flat | page background |
| 1 | `--surface-card` + hair border | cards, panels |
| 2 | `--surface-card-2` | nested cells, code pills, metric boxes |
| 3 | hover/active tone + strong border | interactive cards |
| 4 | translucent dark + blur + soft shadow | sticky header |
| 5 | `--surface-overlay` + 32px radius + `--shadow-lift` (0 24px 64px rgba(0,0,0,.5)) | modals |

No gratuitous inner glows. The one allowed accent glow: the "live" status dot and the tier-upgrade CTA.

---

## 7. Do's and Don'ts

### Do
- Numbers in JetBrains Mono, tabular, right-aligned in tables.
- Every status chip carries an icon or glyph + word (✔/✘/⚠/▲/●/★/❤).
- Uppercase 11px micro-labels with 0.07em tracking above every data field.
- Pair each tier with its fixed accent color everywhere (pricing, matrix, badge, user pill).
- Group related reads into cards; leave 16-24px aria gaps; one idea per card.
- Add a `view-head` to every panel — title, one-line subtitle, right-aligned meta chip.
- Use `--warn`/`--neg` tints *behind* colored text for legibility on dark.
- Provide `@media (prefers-reduced-motion)` to disable pulse/scroll animations.
- Sticky, subtle table headers; empty rows show friendly guidance, not blank data.

### Don't
- Don't use color alone — always pair with text/icon (accessibility + terminal convention).
- Don't stack more than 3 accent colors in one card (brand + one status + tier).
- Don't animate numbers decoratively; a ticker updates values silently in place.
- Don't render more than ~8 KPI cells in a row on desktop (cognitive load).
- Don't put glowing gradients on cards; restraint is the premium signal.
- Don't use light mode. Don't use default `system-ui` for numbers.
- Don't hide tier logic in CSS — masked picks stay masked by the server and are marked `is_locked` client-side for the blur overlay.

---

## 8. Responsive Behavior

- ≥1120px: full layout, 2-col card grids, centered rail nav.
- 1080–1120: picks grid collapses to 1 col; forecast stays 2 col until 1120.
- ≤1080px: header wraps (brand top, nav rail below), ticker absent.
- ≤760px: single column everything; KPI row → 2 cols; forecast/pick cards stack; tier matrix converts per-row to stacked "grant cell" layout; tables scroll horizontally inside `.table-responsive`; tab bar scrolls (overflow-x auto, no wrap).
- ≤480px: KPI row → 1 col; buttons become full-width (block) in action areas; reduce type: hero 28px, view title 20px.
- Touch targets ≥40px at mobile breakpoints; card hovers become no-ops (no lift).

---

## 9. Agent Prompt Guide

- Primary colors: `--brand-400 #ccff00` neon lime actions; `--pos #34D399` gain/low; `--warn #f97316` medium/vip; `--neg #FB7185` loss/high. Canvas `#070a08`, cards `#0c130f` / `#111a14`, hair borders `rgba(255,255,255,0.08)`.
- Key type rules: Inter UI + JetBrains Mono for every number (tabular); 11px caps micro-labels; view titles 24/800; cards 13.5px body.
- Core component rules: 14px-radius cards on tone surfaces, 1px hair borders, no glass; pill chips for tags; brand-filled primary buttons 40px; result/uncertainty chips always icon+word.
- Page structure: header (brand/nav/auth) → optional ticker → `main.container` → `.view-head` per panel → KPI/card grids → tables in `.table-card`. Max 2 accent colors per card.
- Short prompt: "Dark institutional betting terminal: obsidian surfaces, cyan primary, mono numerics, uppercase micro-labels, grouped card grids, icon+word status chips, tier accents 1emerald/2cyan/3gold."