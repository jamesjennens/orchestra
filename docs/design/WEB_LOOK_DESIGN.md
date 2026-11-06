# Website look: a design system (kittrial-5bb.523)

Base: main `b08ca8d4`. Scope is the look only — type, spacing, colour, density,
chips, tables, forms, buttons, empty/loading states, focus, dark mode, the
375 px layout. Navigation, rail, tabs and routes are kittrial-5bb.522's and are
untouched here. Everything below is a proposal for review: no product code has
changed.

**How to look at it.** `docs/design/web-look/` holds four mock pages with
made-up data — `my-work.html`, `tasks.html`, `task.html`, `agents.html` — built
from the real views' DOM (`web/js/views/*.js`), styled by the repo's own
`web/css/app.css` plus one delta file, `docs/design/web-look/proposed.css`.
Open them from the repo root; a tag in the corner marks them as mock-ups.
`img/` holds before/after screenshots of each page at 1280 and 375 px, light,
plus the Tasks page in dark: `before-*.png` is app.css alone, `after-*.png` is
app.css + proposed.css — identical DOM, only the stylesheet differs.

The defect that started this task: in the Tasks table the state chip
("Awaiting review", "Approved · not integrated") ran into the who-acts chip
beside it and was clipped. It is reproduced and measured in §6.

## 1. Type

One scale, five steps. Nothing on the site is smaller than 12 px except the two
places that name a structure twice over (table heads, rail labels), which keep
the 11 px caps idiom (`.502` §2: "a lot of tiny caps"; the fix is fewer uses,
not a smaller font).

| token | size | used for | was |
|---|---|---|---|
| `--t-2xs` | 11 px | table heads, rail labels (caps) | 11 px |
| `--t-xs` | 12 px | secondary text, de-capped labels | 12 px |
| `--t-sm` | 13 px | muted body, meta | 13 px |
| `--t-md` | 14 px | body | 14 px |
| `--t-2xl` | 24 px | h1 | **26 px** |

Rules. `th` goes 11→12 px caps. `.fact-name`, `.resume-label`, `.req-part > h3`
lose their caps transform — they sit next to values that are already 12–14 px,
so caps at 11 px double-encode "this is a label". h1 gets
`overflow-wrap: anywhere` so a long task title can never be squeezed to one
letter per line at phone width.

## 2. Spacing

A 4 px scale replaces the ad-hoc 5/6/9/10/14/18 values, in `.502` §2's density
direction ("loose panels and tight rows" — the panels tighten, the rows get
room to wrap):

`--s-1` 4 · `--s-2` 8 · `--s-3` 12 · `--s-4` 16 · `--s-5` 20 · `--s-6` 24 · `--s-8` 32

| where | now | was |
|---|---|---|
| `.main` (≥761 px) | 20/28/48, gap 16 | 24/32, gap 20 |
| `.stack`, `.detail` | 16 | 20 |
| `.rail` | 14 | 18 |
| `.summary` | 10/14 | 12/16 |
| `th` / `td` | 7/12 · 8/12 | 8 · 9 |

Phone `.main` padding (`16px 16px 40px`, app.css:341) is kept as-is: it is
already on the scale. The wide rule is scoped to `min-width: 761px` — an early
unscoped draft overrode it, squeezed `.main`'s content to 319 px, and the
340 px agent-card grid floor (app.css:299) then pushed cards past the viewport.
That failure is now impossible twice over: the padding override is scoped, and
the grid floor is capped — `minmax(min(340px, 100%), 1fr)` — so a card can
never exceed its container at any width.

The rail is sticky at `100vh`, so on a page taller than the viewport its
background — and the column divider — stopped at the fold (step-2 fix 2;
already so on main). The shell now paints the full-height column itself: a
surface → line → transparent gradient exactly `var(--rail)` wide, scoped
≥761 px. Pixel-verified below the fold on the tall task page: rgb(255, 255,
255) — the surface — where the ground `#f5f7fa` showed before.

## 3. Colour

Tokens are unchanged except one AA fix. Every pair below is measured (WCAG
2.1 relative luminance; AA = 4.5:1 for text, 3.0:1 for graphics).

**Light**

| pair | ratio | | pair | ratio |
|---|---|---|---|---|
| body on surface | 16.29 ✓ | | chip ok tone on ok-soft | 4.64 ✓ |
| secondary on surface | 10.45 ✓ | | **chip warn tone on warn-soft** | **4.14 ✗ → 4.76 ✓** |
| muted on surface / ground / surface-2 | 5.70 / 5.31 / 5.06 ✓ | | chip crit tone on crit-soft | 5.49 ✓ |
| accent on surface / ground | 5.90 / 5.50 ✓ | | chip accent tone on accent-soft | 4.99 ✓ |
| chip text on idle-soft | 9.13 ✓ | | primary btn text on accent | 5.90 ✓ |
| | | | warn / ok / crit as text | 5.38 / 5.33 / 6.57 ✓ |
| | | | idle dot (graphic, needs 3.0) | 3.66 ✓ |

The one failure: light `--warn: #a86400` (app.css:16) measured 4.14:1 as
tone-on-soft chip text at 12 px bold. **Changed to `#9a5c00`**: 5.38:1 on
white, 4.76:1 on warn-soft. Same hue, slightly deeper.

**Dark** — all pairs pass, no change: body 14.14, secondary 10.87, muted
6.44/7.02/5.87, accent 6.38/6.95, chip text 9.30, ok tone 6.47, warn tone
6.77, crit tone 5.76, accent tone 5.35, primary btn (accent-ink on accent)
6.95, warn/ok/crit as text 7.80/7.73/6.25, idle 5.70, toast 15.40.

## 4. Chips and badges

A chip is tone + text, never a layout device. Rules:

1. A chip wraps: `white-space: normal`, `overflow-wrap: anywhere` — at spaces
   first, then anywhere. Nothing inside a chip is `nowrap` except its tone dot
   (`flex: none`), which keeps its own line box.
2. A chip obeys its container: `max-width: 100%`.
3. A state cell keeps `table-cell` display in the wide layout. Overriding it
   to `grid` (the r1 draft) shrank the cell box to its content, so the row's
   divider drew just under the chip, mid-row — a short line floating beside
   "Awaiting review" (caught in review, step-2 fix 1). Chips sit in the cell's
   normal flow; only the phone layout's stacked rows — where the `tr`, not the
   `td`, carries the border — make a cell a grid.
4. `who-acts` in a cell is plain wrappable text with a `→ ` prefix, not a
   chip-shaped box — it names a person, the chip names a state.
5. Long values (`overflow-wrap: anywhere` on h1; wraps everywhere else) mean no
   label, name or title can force a container wide.

## 5. Tables

1. **No chip overlap, no clipping, at any width.** Measured, not eyeballed
   (bounding boxes, headless Chromium; the same measurement becomes the build's
   test): at 1280 and 900 px on the Tasks table —
   before: 4 of 6 state chips bad, worst paints **95.4 px past** its 120 px
   cell into the who-acts cell; after: **0 bad**, every chip ends ≥12 px inside
   its cell. At 375 px on My work: **0 bad**, and the page no longer scrolls
   sideways. (Full numbers in §6.)
2. **Column widths that fit the room.** At ≥761 px the tables' `min-width`
   (900/1000 px, app.css:429/434) is dropped: columns are `state 168 px`
   ("Awaiting review" at 160 px and shorter labels stay on one line; composed
   states fold at the space), `updated 96 px` right-aligned (fits
   "Nov 6, 01:12"), title ≥300/280 px flexible, next ≥200 px flexible.
3. **Long text wraps** in every cell; titles clamp to three lines as today.
4. **Numbers and times right-align**: `th.num, td.num { text-align: right }`
   plus the updated column.
5. **One divider per row.** Every cell of a row shares one bottom edge, in
   every table, so the divider always draws on the row line. Measured: bottom
   edge spread per row ≤ 0.5 px after (the r1 draft's grid override left the
   state cell 20 px short of its row). The measurement is part of the
   chip-overlap check in §6 and of the build's test suite.
6. **Phone (≤760 px): both tables stack to cards.** The project table already
   stacked (`stack-narrow`, app.css:450-456) but put the state chip *beside*
   the title, so a 158 px chip squeezed the title to a few characters a line;
   now title, state and next each take a full row. The My work table
   (`task-list-work`, web/js/views/work.js:8) never stacked — it now gets the
   same treatment in CSS, no view change needed.

## 6. The defect, reproduced

Mechanism: `.chip { white-space: nowrap }` (app.css:196) inside a fixed 120 px
`task-state-column` (app.css:431) under `table-layout: fixed` — a 203 px chip
("Approved · not integrated") has nowhere to go but sideways, over the
who-acts cell (web/js/views/project.js:571-573).

Measurement (bounding boxes; "bad" = chip bleeds into the neighbour cell, is
clipped by the table wrapper, extends past its own cell, or — stacked — the
page scrolls sideways):

| view | rows | bad | worst chip past cell |
|---|---|---|---|
| before 1280 | 6 | 4 | +95.4 px |
| **after 1280** | 6 | **0** | −12 px |
| before 900 | 6 | 4 | +95.4 px (also clipped by wrapper) |
| **after 900** | 6 | **0** | −12 px |
| before 375 (My work) | 4 | 0 | −12 px (chips fit; the *titles* were the unreadable squeeze) |
| **after 375 (My work)** | 4 | **0** | −12 px, no sideways page scroll |

Screenshots: `img/before-tasks-1280-light.png` shows the collision;
`img/after-tasks-1280-light.png` shows the same table wrapped and separated.

## 7. Empty and loading states

One idiom, no blank panels: an empty list is a block `.empty` with one line of
muted text ("No tasks yet — created tasks appear here"), now with a dashed
border so the region reads as deliberate. An empty *group* inside a page gets
a one-line `.empty-line`. Loading is a panel with muted text and `aria-busy`
on the region, never a blank. (Existing behaviour; the note pins it as the
rule so the build keeps it.)

## 8. Focus, keyboard, dark, motion

Focus stays a 2 px ring at 2 px offset on every control, row link and row
title; nothing removes it or restyles it to invisibility. Dark mode keeps
app.css's token set (all pairs pass, §3) driven by
`documentElement.dataset.theme`; the mock-ups exercise it. Motion: the rail
drawer's 0.18 s transform stays and stops under `prefers-reduced-motion`;
nothing else animates, pulses or fades in — this proposal adds zero motion.

## 9. The delta, rule by rule

`docs/design/web-look/proposed.css`, in order: (1) type — `--t-2xl` 26→24,
`th` 12 px caps, de-cap three label classes, h1 wrap; (2) spacing — the 4 px
scale, scoped wide `.main`, tighter `.stack`/`.summary`/`td`; (3) colour —
`--warn: #9a5c00` light only; (4) chips — §4 rules; (5) tables — §5 rules,
including the `.agent-grid` floor cap; (6) the shell's full-height rail column
(§2, step-2 fix 2); (7) `.empty` dashed border, `.loading`
size; (8) nothing in dark/motion, by design. Every rule uses app.css's own
tokens, adds no external asset and needs no script; the build slice that lands
it should need `web/css/app.css` plus small class additions in
`web/js/views/*.js` (a `num` class on numeric columns).

## 10. Not changed

Rail, tabs, routes, page structure and every navigation idea from `.502`/`.522`
are out of scope. No view logic, no data, no new elements beyond the `num`
class the build adds. Windows was not run; measurements are Chromium on Linux.
