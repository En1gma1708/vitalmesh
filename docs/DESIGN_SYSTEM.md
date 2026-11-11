# VitalMesh design system

Applies to the five pages in `services/api/src/vitalmesh_api/static/` (landing and sign-in, dashboard, account, clinician console, engineering console) and the 404 page. One stylesheet, `static/vm.css`, is injected by the server into each page's nonce-tagged style block (the content-security policy allows no external stylesheet or font). Decision record: D106.

## Principles
1. **Light only, on purpose.** People with health problems (low vision, migraine, tired eyes) read a light page better than a black one. There is no dark theme.
2. **Contrast is measured, not guessed.** `services/api/tests/test_design_contrast.py` reads the real tokens and fails below WCAG AA (text 4.5:1, chart marks 3:1).
3. **Each body signal has its own hue** (like channels of a patient monitor) and one accent is reserved for actions and the brand. Colour is never the only carrier of meaning: every chart has a label, a legend and readable values.
4. **Calm.** Motion exists only where it shows what is happening (see Motion). Sentence case everywhere; no uppercase labels.
5. **No invented content.** No testimonials, counts, awards or claims; the product says what it does, and says "not medical advice".

## Colour tokens (navy and cream, the default)
| Token | Value | Use |
|---|---|---|
| `--bg` | `#f7f3e8` | page |
| `--card` | `#ffffff` | cards and panels |
| `--card-2` | `#f1ecdd` | insets, hover |
| `--line`, `--line-2` | `#e3dccb`, `#cfc6b0` | hairlines, control borders |
| `--fg` | `#16213e` | text |
| `--muted` | `#505a70` | secondary text (also used for hints and the footer) |
| `--accent`, `--on-accent` | `#1b3a72`, `#ffffff` | primary actions, links, brand |
| `--accent-soft` | `#e3e9f5` | selected, notices |
| `--warn`, `--warn-soft` | `#b3261e`, `#fbe7e4` | errors and refusals only |
| `--ok` | `#1e7a4e` | released, checked |
| `--c-sleep` `--c-active` `--c-energy` `--c-weight` | `#5b4bc4` `#2e7d32` `#a35f00` `#475569` | signal hues: sleep violet, activity green, calories amber, weight slate |
| `--t0`..`--t3` | slate, blue, ochre, navy | answer tiers in the operator console |

Alternative still selectable with `data-palette="forest"` (beige page `#f3ecdd`, forest accent `#1f5f46`); one of the two will be removed once chosen.

Measured contrast (navy and cream): body text on page 14.3:1, on cards 15.9:1; muted text 5.9 to 6.9:1; button text on accent 11.1:1; error text 6.5:1; chart marks 5.0 to 7.6:1 (forest: 12.6 and 14.6; 5.5 to 6.7; 7.5; 6.4; 4.9 to 7.5).

## Type
System font stack (`system-ui`, Segoe UI Variable and others; nothing is downloaded), tabular figures for numbers, a monospace face (`ui-monospace`) for the live trace.
| Role | Size / weight |
|---|---|
| Page title (h1) | clamp(22px, 2.4vw, 28px) / 650, tight tracking |
| Section title (h2) | 15px / 620 (the question panel: 17px) |
| Group label, h3 | 13px / 600, muted |
| Body | 15px / 1.55 |
| Secondary (`.sub`) | 13px, muted |
| Headline figure (KPI) | 26px / 650 |
Line length for running text is capped near 68 characters.

## Spacing, shape
Gaps 8, 10, 12, 14, 16, 18, 24, 28 px. Cards: radius 14, padding 18 (panels 22 to 24). Controls: radius 9. Pills 999. Borders 1px `--line`; focus ring 2px `--accent` with 2px offset. Containers: content up to 1240px beside a 224px rail; sign-in panel 440px (onboarding 560px); 404 panel 560px. Side gutter at least 16px; the page never scrolls horizontally (checked at 320, 360, 390, 768 and 1440 px).

## Components
Card and panel; button (primary, `.ghost`, link-style `a.btn`); form controls (fields, selects, checkboxes with the whole label as the tap target, file inputs); status tag with a dot (normal, high, low); notice and banner; toast (polite for success, alert for errors); live trace timeline (monospace, the last step pulses, green on release); pipeline (six stages lit by the real step events; a stage not announced on a run is dimmed, with no claim either way); goal ring (draws in once, number counts up once); chart with tooltip (pointer, touch, left and right arrow keys; the chart stays a labelled image); headline figure with a plain change line (no good-or-bad colour: it depends on the goal); loading skeleton; empty state with first steps; stepper with check marks; left rail that becomes a bottom bar on phones; account menu; designed 404 page.

## States
Hover: slight brightness or the inset colour. Focus: visible 2px ring on every control and on each chart. Disabled: 55% opacity and a progress cursor. Error: `--warn` text near the field, `role="alert"`. Success: inline `role="status"` text or a toast. Loading: skeletons that match the final shape. Empty: a sentence saying what happens next and links to the first steps.

## Motion (all of it)
Heartbeat sweep on the landing page; pulse on the active trace step and pipeline stage; a beam along the connector into the active stage; goal rings and numbers draw in once; skeleton shimmer; the account menu. A global rule switches every animation and transition off under `prefers-reduced-motion`. Nothing animates on scroll, and there are no page transitions.

## Rules for new pages and changes
- Put the marker `/*@vm.css*/` first inside the page's `<style>`; keep `<style>` and `<script>` free of attributes (the server adds the nonce); never write angle-bracket tags in `vm.css`.
- Give every page a unique title (10 to 70 characters), a description (50 to 170), an inline favicon and, for any private page, `noindex`. A test checks all of it.
- No external fonts, scripts or images. Every internal link and anchor must resolve (a test follows them).
- A new colour pair must pass the contrast test before it ships. Check every UI change in a real browser at phone width (390px) and on the real stack under the strict policy, not only in a preview.

## Known differences
The clinician and engineering consoles keep their own layout rules (tables and dense panels) on top of the shared tokens. Light mode is the only mode; the palette choice (navy or forest) is open.
