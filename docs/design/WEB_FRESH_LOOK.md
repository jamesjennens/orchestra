# A fresh look for the website (design note, kittrial-5bb.525)

**Status:** design only. No product code changes. The mock-ups are in `docs/design/web-fresh/`, sharing one stylesheet, `fresh.css`. There are 24 screenshots in `web-fresh/shots/`, plus a before/after pair of Tasks, and all of them are on `web-fresh/contact-sheet.html`.
**Base:** main at b08ca8d4.
- **Why today's token names:** .523's design note could not be read from this lane, so every token below uses today's names in `web/css/app.css:2-36`. Section 8b maps them onto .523's names (r1), now readable.
- **Mapping to .523:** the token table (section 8) lists today's value next to the proposed light and dark values, so it can be mapped onto .523's names when they arrive.
- **Navigation:** the rail and tabs follow .522 (PROJECT_CROSS_SECTION_DESIGN.md §3, §3.2, §3.3).

## 1. The identity, in a paragraph

Orchestra is a shared record that people and their agents work from, so it should feel like a well-kept score: calm, exact, and clear about whose turn it is. Today the site looks generic but sound:
- a cool blue-grey palette (app.css:3-22) with one stock blue accent (app.css:11);
- one sans for everything;
- a box around every panel (app.css:177-178);
- 11 px uppercase labels in 5 rules (app.css:89, :183, :216, :286, :307).

The fresh look keeps the bones and changes the voice:
- **Paper, not chrome:** a warm paper ground for the rail and a white working surface.
- **A violet-indigo accent used only for interaction:** links, the primary button, the current place, focus.
- **A serif for names and numbers:** the wordmark, page titles and each page's key number. The system sans stays for reading.
- **Headings and hairlines instead of boxes.**
- **State colours that appear only where a state is meant.**
- **Every page answers three questions at the top:** what this is, the one number that matters, and what to do next.

## 2. The mark and the wordmark

- **The mark (inline SVG, 24×24):** a rounded tile in `--accent` holding three bars that start at different times and overlap. It reads as parts entering a score in turn, and also as a small timeline of work. That is the same idea as today's five vertical bars (`web/js/dom.js:42-56`), made simpler and solid enough to work as a 16 px favicon.
  - The bars use `--accent-ink`, so in dark mode they are dark on a light-violet tile with no extra artwork.
  - The source is the `MARK` string in the mock-ups. It needs no image file.
- **The wordmark:** "Orchestra" in the display serif, weight 600, letter-spacing −0.01em: 19 px in the rail, 24 px on the sign-in page, 22 px on the phone. Today it is the body sans at 16 px/650 (app.css:86).
- **The sign-in page extends the mark:** a large "score" of grey bars with one accent bar (`signin.html`). It is drawn with SVG `rect`s, so there are no assets. It is the only decorative element on the site.

## 3. Type: pairing and scale

- **Reading text:** today's system sans stack (app.css:26) plus `"Noto Sans", "DejaVu Sans"` before the generic fallback.
- **Display (new role):** `Charter, "Bitstream Charter", "Sitka Text", Cambria, "Iowan Old Style", Georgia, "Noto Serif", "DejaVu Serif", serif`, which gives Charter on macOS, Sitka or Cambria on Windows and Noto Serif on Android.
  - **Used only for:** the wordmark, the `h1`, the key number and the sign-in headline. Section headings (`h2`) stay sans, so the serif stays a signature and not a texture.
- **Mono:** today's stack plus `"DejaVu Sans Mono"`. Task ids, usernames and file paths are mono, so a reader can tell an id from prose.
- **Vendoring (proposed only, not added):** if James wants the same serif on every machine, vendor **Source Serif 4** (SIL Open Font License 1.1, Adobe) as one variable WOFF2 (about 120 KB, roman only) with its `OFL.txt` next to it, served same-origin. It needs no CSP change.

| step | token | size | used for |
|---|---|---|---|
| xs | `--t-xs` | 12 | table heads, chips, meta, counts |
| sm | `--t-sm` | 13 | sub-nav, crumbs, notes, buttons |
| md | `--t-md` | 14 | body **and table cells** (today tables are 13: app.css:181) |
| lg | `--t-lg` | 16 | section headings (`h2`, sans 650), sign-in inputs |
| xl | `--t-xl` | 20 | (spare) |
| 2xl | `--t-2xl` | 28 (was 26) | `h1`, serif 600; 24 on the phone |
| 3xl | `--t-3xl` NEW | 36 | the key number, serif, tabular figures; 28 on the phone |

**Rule:** no 11 px uppercase labels. Table heads, rail labels and fact names become 12 px sentence case in `--muted`, weight 600. Today's uppercase labels are at app.css:89, :183, :216, :286, :307.

## 4. Space and a calmer density

- **Spacing scale (new tokens):** `--s-1..7` = 4/8/12/16/24/32/48. The mock-up CSS uses these values directly. The product would use the tokens.
- **Gaps between parts of the page:**
  - Sections are 36 px apart on desktop and 28 px on the phone (today's `.stack` is 20 px: app.css:123).
  - Inside a section, the heading sits 12 px above its content.
- **Content width:** at most `--measure` 1120 px, with the main padding at 32/40 px. Today the main has no maximum width (app.css:106).
- **Rows:** cells are 12 px top and bottom (today 9 px: app.css:186), vertically centred. Rows get roomier while pages get shorter, because the boxes go.
- **Fewer boxes:** a section is a heading, a hairline and a table. A box is kept only where it means something:
  - the contribution under review;
  - the four-fact strip;
  - empty and error states.
  - Today every list sits in a `.panel` with a `.panel-head` (app.css:177-178).
- **Phone (375 px):**
  - The rail becomes a top bar: mark, where you are, menu.
  - Header: the title, then the key number and its label on one line, then the action.
  - **Table rows stack:** each row puts the title on its own line, then the state and the time on one line, then who acts. No table scrolls sideways. All six pages were measured at 375 px, and none is wider than the screen.

## 5. Colour with a purpose

| role | tokens | rule |
|---|---|---|
| Neutral | `--ground --surface --surface-2 --ink --ink-2 --muted --line --line-strong --field-line` | All text and structure. A warm grey with a trace of violet, so the accent belongs. `--ground` is the rail and quiet wells; `--surface` is the working page. |
| Accent | `--accent --accent-ink --accent-soft --focus` | **Interaction only:** links, the primary button, the current nav item, current tab, focus, the "yours" bar on rows waiting on you, "**You**" in who-acts, the counts on My work and Agents that are waiting on you. Never a state. |
| State | `--ok --warn --crit --info --idle` (+ `-soft`) | Only on a chip, a dot or a fact that names a state. `info` = in progress; `warn` = waiting for someone (awaiting review, needs setup); `crit` = sent back, failed, P0; `ok` = done, approved, online; `idle` = open, not yet. A state always has a word next to its colour. |

**The fault this fixes.** Today the accent is also a state. "In progress" and "Awaiting review" are drawn with the `accent` chip (`web/js/ui.js:21`, `:32`; `app.css:204`), and that is the same blue as links, buttons and the current nav item (app.css:95). The fix:
- a new `--info` takes "in progress";
- "awaiting review" moves to `warn`.

**A second fault.** Input, select and button borders use `--line-strong` (app.css:143, :157), which is **1.62:1** on white (1.71:1 dark). That is below the 3:1 that WCAG 1.4.11 asks for the edge of a control. A new `--field-line` is 3.56:1 (3.37:1 dark).

## 6. Patterns

**The page header** (every page; `.page-head` in fresh.css):
- **Layout:** a three-column grid, `title | key number | action`, over a hairline. Crumbs sit above it, and a lede or a "Showing:" line sits under the title.
  - Today's `pageHead` has crumbs, title, lede and actions, but no number (`web/js/ui.js:64-72`).
  - The Tasks page's `h1` is the project's name, not "Tasks", and its summary strip has chips without numbers (`web/js/views/project.js:600-610`).
- **The key number is one number with a short label:** "17 open · 4 wait on you", "5 wait on you, across 3 projects", "2 days waiting for your review".
- **The action is the one next step:** New task, Open the oldest review, Approve.
- **Filtered pages (.522 §2):** they carry `Showing: Alpha · change · show all projects` under the title.

**Tables** (`table.stacks`):
- **Header row:** 12 px sentence-case heads under a strong top rule.
- **Title cell:** the title in 600 weight, then a mono id and a priority on a sub-line.
- **State column:** one chip.
- **Who acts next:** written out, with "**You**" in the accent.
- **Time:** right-aligned, tabular.
- **Rows waiting on you** carry a 3 px accent bar on the left. That is the "at a glance" cue: you can find your rows without reading. My work does not use the bar, because every row there is yours.
- **Greyed rows** are possible-but-not-yet (your agents not in this project).
- **On the phone:** rows stack as described in §4.

**Empty, loading and error states** (`.state`, `.skeleton`):
- **Empty:** a dashed well on `--ground` with a round icon, a title that says what would be here, and one sentence on how it gets there. Examples: "Nothing in review", "Nothing to confirm", "No files attached". Today there are two styles: `empty()` is a 32 px centred block (`ui.js:74-76`; app.css:251), and `.empty-line` is one line.
- **Loading:** a skeleton in the shape of the content that will come, here three timeline rows. It has `role="status"` and a hidden "Loading activity…" for screen readers. The shimmer stops under `prefers-reduced-motion`.
  - It replaces today's text-only `loading()` (`ui.js:78-80`; app.css:253).
  - It also replaces the fixed 6 rem "Loading your agents…" box (`web/js/views/agents.js:199`), which .502 measured shifting the layout by 0.589 on the phone.
- **Error:** the same well on `--crit-soft` with a 4 px `--crit` left rule and a warning icon. It holds:
  - what failed, in the reader's words ("Could not read Gamma");
  - what is still true ("the other projects are complete");
  - the request id in mono;
  - **Try again.**
  - It is used inline where only one part failed, and never as a whole-page replacement when the rest loaded. Today's `errorState` is a centred block in a panel (`ui.js:114-120`).

## 7. What each page gains

| page | today | fresh |
|---|---|---|
| **Sign in** (`signin.html`) | A 380 px card centred on grey with a 16 px wordmark (`web/js/views/auth.js:36-44`; app.css:263-266) | Split screen: a brand side on paper (mark, serif headline "Every part, in its turn.", one sentence on what Orchestra is, the score motif), and a form side with 42 px fields, a 44 px button, "Have a reset code?" next to Password, and one footer line. On the phone the brand side folds to the mark above the form. |
| **My work** (`my-work.html`) | `h1` plus a long lede (`web/js/views/work.js:61`), then three separate tables and repeated agent cards | Key number "5 wait on you" and the action "Open the oldest review". One table with a "Why it is here" column and an All / Reviews / Assigned switch. An inline error when one project fails. A designed empty state for "Delivered by you". Agents as one line each. |
| **Tasks** (`tasks.html`) | `h1` = project name; a summary with no numbers (project.js:600-605); chips overflow into the next column (`before-tasks-1280-light.png`) | `h1` "Tasks", key "17 open · 4 wait on you", "+ New task". Filters with counts (Open 17 · Closed 42) and "Anyone / You act next". Rows waiting on you are marked. State chips use the new state roles. The phone shows stacked rows. |
| **A task** (`task.html`) | Crumbs Projects / project / id (`task.js:356`); the review panel is the third panel down | Crumbs Alpha / Tasks / id. The header carries the state, "You act next", the key "2 days waiting for your review" and **Approve / Send back** at the top. Four facts (Claimed, Delivered, Review, Integrated). The contribution box has its own action bar. A skeleton for Activity, a designed empty state for Files, and a Details list beside them. |
| **Agents** (`agents.html`, .522 §3.2) | My agents as cards (app.css:299-301; `agents.js:509`) | A table: Agent, Owner, Projects, In Alpha, Last seen (online dot). "Showing: Alpha · change · show all projects". The key "1 needs you". "Add to Alpha" in the row for your agents that are not in it. Other owners' agents are read-only. |
| **Project › People & Agents** (`project.html`, .522 §3.3) | Members & settings, 2,751 px with forms open (.502 report §2) | The project header with tabs (Tasks … People · Agents · Settings · Set up · How). **People in Alpha** with "+ Add person", a role select and Remove. **Agents in Alpha** with the "Include my agents not in Alpha" switch (shown on: greyed rows with "Add to Alpha") and "+ Add agent". In .522 People and Agents are two tabs; the mock-up shows both sections under the People tab so that one screenshot covers both. |

## 8. Tokens changed or added (today's names; superseded for the build by §8b)

There are 40 rows: **27 changed, 13 new**. A value of "same" means dark equals light.

| token | today (light / dark) | proposed light | proposed dark |
|---|---|---|---|
| `--ground` | #f5f7fa / #0f141a | #f6f5f2 | #121116 |
| `--surface` | #ffffff / #161d25 | #ffffff | #1a1920 |
| `--surface-2` | #eef2f6 / #1c2530 | #efede8 | #24232b |
| `--ink` | #17212b / #e5ebf1 | #1d1c21 | #ecebf0 |
| `--ink-2` | #33414f / #c6d0da | #3b3942 | #cac7d1 |
| `--muted` | #5a6878 / #93a1b0 | #605c67 | #a19daa |
| `--line` | #dce2e9 / #26313d | #e6e3dd | #2c2a33 |
| `--line-strong` | #c3ccd6 / #364453 | #d3cfc7 | #3b3943 |
| `--accent` | #2f5bd3 / #7d9cf0 | #4b3fc0 | #aaa4ff |
| `--accent-ink` | #ffffff / #0f141a | #ffffff | #121116 |
| `--accent-soft` | #e6ecfb / #1f2a44 | #ecebfb | #2b2852 |
| `--focus` | #2f5bd3 / #9bb4f5 | #4b3fc0 | #c4c0ff |
| `--ok` | #1e7a4b / #5cc28d | #1b6b41 | #6fcf97 |
| `--ok-soft` | #e3f3ea / #16301f | #e2f1e7 | #16301f |
| `--warn` | #a86400 / #e2a44a | #8a5200 | #f0b45a |
| `--warn-soft` | #fbf0dc / #33260f | #fbefd9 | #35270f |
| `--crit` | #b42318 / #f07a70 | #b0251d | #ff8f85 |
| `--crit-soft` | #fbe6e4 / #3a1a17 | #fbe7e5 | #3d1b19 |
| `--idle` | #7b8794 / #8a97a5 | #5f5b66 | #aaa6b2 |
| `--idle-soft` | #edf0f3 / #212a34 | #eeece8 | #2a2930 |
| `--shadow` | 0 1px 2px …/.06, 0 4px 16px …/.06 | 0 1px 2px rgba(29,28,33,.05), 0 8px 24px rgba(29,28,33,.06) | 0 1px 2px rgba(0,0,0,.5), 0 8px 24px rgba(0,0,0,.35) |
| `--radius` | 6px | 8px | same |
| `--rail` | 232px | 248px | same |
| `--font` | system sans stack | same + `"Noto Sans", "DejaVu Sans"` | same |
| `--font-display` | Segoe UI Variable Display, … (sans) | Charter, "Bitstream Charter", "Sitka Text", Cambria, "Iowan Old Style", Georgia, "Noto Serif", "DejaVu Serif", serif | same |
| `--mono` | Cascadia Mono, … | same + `"DejaVu Sans Mono"` | same |
| `--t-2xl` | 26px | 28px | same |
| `--field-line` NEW | — (borders used `--line-strong`) | #8a8691 | #6f6b79 |
| `--info` NEW | — (`accent` was used) | #0a6178 | #71c7e0 |
| `--info-soft` NEW | — | #dff0f5 | #13303a |
| `--radius-sm` NEW | — | 6px | same |
| `--measure` NEW | — | 1120px | same |
| `--t-3xl` NEW | — | 36px | same |
| `--s-1` … `--s-7` NEW (7) | — | 4 / 8 / 12 / 16 / 24 / 32 / 48 px | same |

Unchanged: `--accent-ink` light, `--surface` light, `--t-xs`…`--t-xl`.

**Two product mappings follow from this** (for the slice that applies it, not done here):
- `ui.js:21` `'awaiting-review'` → `warn`;
- `ui.js:32` `in_progress` → `info`.

## 8b. The fresh look on .523's frozen names

**Source and status:**
- .523's groundwork is approved at c6c8eb3e on b08ca8d4, and its token names are frozen. The frozen list comes from the coordinator's summary of .523 r2.
- I read .523 r1 (`WEB_LOOK_DESIGN.md` = W, `web-look/proposed.css` = P, at `/srv/review-snapshot/k523/r1-design/`). Neither c6c8eb3e nor `tests/test_site_look_523.py` is readable from this lane, so the build re-reads both first.
- **This section supersedes §8 for the build.** §8 keeps the original proposal against today's names.

**Rule:** every fresh value goes on a frozen name. A size or colour the frozen set lacks is in *Proposed additions* below; nothing is added silently. Values that are on .523's scale are snapped to it (last table).

**On frozen names** (40 names; "=" means the build keeps .523's or today's value):

| frozen name (.523 cite) | fresh light | fresh dark | note |
|---|---|---|---|
| `--ground` (W §3 l.69) | #f6f5f2 | #121116 | |
| `--surface` | #ffffff | #1a1920 | |
| `--surface-2` | #efede8 | #24232b | |
| `--ink` | #1d1c21 | #ecebf0 | |
| `--ink-2` | #3b3942 | #cac7d1 | |
| `--muted` | #605c67 | #a19daa | |
| `--line` | #e6e3dd | #2c2a33 | |
| `--line-strong` | #d3cfc7 | #3b3943 | dividers only; control edges use `--field-line` |
| `--accent` | #4b3fc0 | #aaa4ff | interaction only (§5) |
| `--accent-ink` | #ffffff | #121116 | |
| `--accent-soft` | #ecebfb | #2b2852 | |
| `--focus` (W §8 l.166) | #4b3fc0 | #c4c0ff | |
| `--ok` | #1b6b41 | #6fcf97 | |
| `--ok-soft` | #e2f1e7 | #16301f | |
| `--warn` (W §3 l.84-86; P:44) | **= #9a5c00** (.523's) | #f0b45a | Passes on the fresh backgrounds: 4.72:1 on fresh `--warn-soft` and 4.93:1 on `--ground`; see §9. My #8a5200 is withdrawn. |
| `--warn-soft` | #fbefd9 | #35270f | |
| `--crit` | #b0251d | #ff8f85 | |
| `--crit-soft` | #fbe7e5 | #3d1b19 | |
| `--idle` | #5f5b66 | #aaa6b2 | |
| `--idle-soft` | #eeece8 | #2a2930 | |
| `--shadow` | 0 1px 2px rgba(29,28,33,.05), 0 8px 24px rgba(29,28,33,.06) | 0 1px 2px rgba(0,0,0,.5), 0 8px 24px rgba(0,0,0,.35) | |
| `--radius` | = 6px | = | Controls, chips and the rail links keep 6. The 8px boxes use a proposed `--radius-lg`. |
| `--rail` | 248px | = | |
| `--font` | today's stack + `"Noto Sans", "DejaVu Sans"` | = | |
| `--font-display` | the serif stack of §3 | = | |
| `--mono` | today's stack + `"DejaVu Sans Mono"` | = | |
| `--t-2xs` (P:11) | = 11px | = | No fresh rule uses it: table heads and rail labels move to `--t-xs`, sentence case. |
| `--t-xs` `--t-sm` `--t-md` `--t-lg` `--t-xl` | = 12 / 13 / 14 / 16 / 20 | = | |
| `--t-2xl` (W §1 l.35; P:11) | = 24px | = | The h1 on the phone, and the sign-in wordmark |
| `--s-1` `--s-2` `--s-3` `--s-4` (W §2 l.49; P:22) | = 4 / 8 / 12 / 16 | = | |
| `--s-5` `--s-6` `--s-8` | = 20 / 24 / 32 | = | There is no `--s-7`. |

**Proposed additions** (8 names; each needs the coordinator's yes before the build adds it):

| proposed | light | dark | why the frozen set lacks it | where it is used |
|---|---|---|---|---|
| `--field-line` | #8a8691 | #6f6b79 | `--line-strong` on white is 1.62:1, so control edges fail WCAG 1.4.11 (3:1); `--line-strong` must stay faint for dividers. | input, select, button, seg and switch borders (app.css:143, :157, :171-173) |
| `--info` | #0a6178 | #71c7e0 | Today "In progress" borrows `--accent` (ui.js:32), so the accent means both "click" and "a state" (§5). | the In progress chip and dot |
| `--info-soft` | #dff0f5 | #13303a | the chip background for `--info` | the In progress chip |
| `--radius-lg` | 8px | = | Boxes that mean something (§4) read softer than controls. | `.state`, the contribution box, the facts strip, dialogs, the sign-in fields |
| `--measure` | 1120px | = | Main has no maximum width today (app.css:106); lines run 1,000+ px at wide screens. | `.page` max-width |
| `--s-12` | 48px | = | The scale stops at 32, and the page foot and sign-in panel need more air. Named in .523's 4 × N scheme. | `.main` bottom padding; sign-in panel padding |
| `--t-3xl` | 28px | = | The desktop h1 of the chosen look is 28. `--t-2xl` is now 24 and stays the phone h1. | h1 ≥761 px; key number ≤760 px |
| `--t-4xl` | 36px | = | the key number is the page's one large figure | key number ≥761 px; sign-in headline (mock-up 34, snapped to 36) |

In §3 and §8 the 36px token was called `--t-3xl`. On the frozen scale it is `--t-4xl`, and 28 is `--t-3xl`.

**Snapped to .523's scale** (the mock-up used literals; the build uses these, with no new token):

| where | mock-up | build |
|---|---|---|
| sections apart | 36 / 28 phone | `--s-8` 32 / `--s-6` 24 |
| `.main` padding | 32 40 64 / 20 16 48 phone | `--s-8` `--s-8` `--s-12` / `--s-5` `--s-4` `--s-12` |
| page head gap / bottom | 8 32 / 24 (18 phone) | `--s-2` `--s-8` / `--s-6` (`--s-4`) |
| `th` / `td` padding | 10 12 / 12 | `--s-2` `--s-3` / `--s-3` |
| task-page column gap, side gap | 40, 24 | `--s-8`, `--s-6` |
| `.state` padding | 18 20 | `--s-4` `--s-5` |
| sign-in panel / form gap | 48 56 / 22 | `--s-12` / `--s-5` |
| wordmark rail / sign-in / phone / topbar | 19 / 24 / 22 / 17 | `--t-xl` / `--t-2xl` / `--t-xl` / `--t-lg` |
| rail gap | 22 | 14, .523's literal (P:32); see open point 1 in plan.md |

**Already done by .523, so kept and not redone:**
- the chip wraps inside its cell (W §4; P:51-58), with only the fresh tone, radius and height on top;
- task tables: fixed layout, min-width 0, a 168 px state column, a 96 px right-aligned Updated column with `num` (W §5; P:70-85);
- both task tables stack at ≤760 px (W §5 l.125-130; P:88-102). Fresh adds only the time, right-aligned on the state row;
- dashed `.empty` (P:108), with the fresh fill, icon and action on top;
- `.loading` size (P:109), replaced in slice (e) by the skeleton but on the same class;
- the `.agent-grid` cap (P:82);
- scoped `.main` padding (P:26-28), with fresh values under the same `min-width: 761px` scope;
- `.page-head h1` overflow-wrap (P:17);
- the rail column painted full height (r2);
- who-acts as plain text (P:60-61); fresh adds `.you` and drops the `→ ` prefix;
- the three de-capped label classes (P:14-16); fresh also de-caps `th` and `.rail-label`.

**Not kept from .523:** its denser values (`.main` 20/28, `.stack` 16, `td` 8). The fresh values above win, because James chose the calmer density.

## 9. WCAG AA: computed contrast for every pair used

The ratios are WCAG 2.x relative luminance, computed by a script from the hex values in `fresh.css`. The script is `/home/opusw/work/t525/scratch/contrast.py`, outside the repo. Text needs 4.5:1; control edges, focus and state marks need 3:1. **All 29 pairs pass in both themes.**

| text / background | used for | light | dark | needs |
|---|---|---|---|---|
| `ink` / `surface` | body, cells, headings | 16.93 | 14.71 | 4.5 ✓ |
| `ink` / `ground` | rail, sign-in side, facts | 15.53 | 15.85 | 4.5 ✓ |
| `ink-2` / `surface` | who-acts, prose, buttons, role tag | 11.35 | 10.47 | 4.5 ✓ |
| `ink-2` / `ground` | rail links, sign-in pitch | 10.41 | 11.28 | 4.5 ✓ |
| `ink-2` / `surface-2` | section counts | 9.70 | 9.33 | 4.5 ✓ |
| `muted` / `surface` | crumbs, table heads, sub-lines, dates, placeholders, greyed rows | 6.51 | 6.58 | 4.5 ✓ |
| `muted` / `ground` | rail labels, empty-state text, fact names | 5.97 | 7.09 | 4.5 ✓ |
| `muted` / `surface-2` | muted text on a hovered row (worst case) | 5.57 | 5.86 | 4.5 ✓ |
| `accent` / `surface` | links, "You", quiet buttons | 7.58 | 7.86 | 4.5 ✓ |
| `accent` / `ground` | links on sign-in side and rail | 6.95 | 8.47 | 4.5 ✓ |
| `accent-ink` / `accent` | primary button, "you" counts | 7.58 | 8.47 | 4.5 ✓ |
| `ink` / `accent-soft` | avatar initials | 14.39 | 11.59 | 4.5 ✓ |
| `surface` / `ink` | pressed segment button | 16.93 | 14.71 | 4.5 ✓ |
| `idle` / `idle-soft` | chip Open | 5.61 | 6.04 | 4.5 ✓ |
| `info` / `info-soft` | chip In progress | 5.99 | 7.24 | 4.5 ✓ |
| `ok` / `ok-soft` | chip Approved | 5.57 | 7.48 | 4.5 ✓ |
| `warn` / `warn-soft` | chip Awaiting review, Needs setup | 4.72 | 7.86 | 4.5 ✓ |
| `crit` / `crit-soft` | chip Changes requested | 5.65 | 6.95 | 4.5 ✓ |
| `ok` / `ground` | fact done | 5.97 | 9.89 | 4.5 ✓ |
| `warn` / `ground` | fact now | 4.93 | 10.18 | 4.5 ✓ |
| `ok` / `surface` | diff + lines | 6.51 | 9.18 | 4.5 ✓ |
| `crit` / `surface` | P0, diff − lines | 6.71 | 7.91 | 4.5 ✓ |
| `ink` / `crit-soft` | error title | 14.24 | 12.93 | 4.5 ✓ |
| `ink-2` / `crit-soft` | error text, request id | 9.55 | 9.20 | 4.5 ✓ |
| `field-line` / `surface` | input, select, button edges (1.4.11) | 3.56 | **3.37** | 3 ✓ |
| `focus` / `surface` | focus ring | 7.58 | 10.24 | 3 ✓ |
| `accent` / `surface` | current tab, "yours" bar, switch on | 7.58 | 7.86 | 3 ✓ |
| `crit` / `crit-soft` | error icon and rule | 5.65 | 6.95 | 3 ✓ |
| `ok` / `surface` | online dot | 6.51 | 9.18 | 3 ✓ |

- **Lowest text pair: 4.72:1**, the Awaiting review chip in light mode (.523's frozen `--warn` #9a5c00 on fresh `--warn-soft`). It was 5.61 with my withdrawn #8a5200. The next lowest is 5.57, `muted` on `surface-2`.
- **Lowest pair overall: 3.37:1**, the control edge in dark, against a 3:1 need.
- **Today, for the same roles:**
  - muted on ground 5.31 / 7.02;
  - link 5.90 / 6.38;
  - control edge **1.62 / 1.71** (fails 1.4.11);
  - idle dot 3.66 / 5.70.
- **Not measured:**
  - the disabled "Previous" button, which is exempt from AA;
  - the online dot of an agent that is not online, which is decorative because the time next to it carries the meaning;
  - native `select` pop-ups, which the browser draws.
- **Colour is never the only signal:** every state has a word, and "yours" rows also say "**You**".

## 10. Limits, and what I could not do

- **.523's note:** it was unreadable when sections 1-10 were written. It has since been read (r1 snapshot), and section 8b maps the tokens onto its names.
- **Fonts in the screenshots:** the capture machine has only DejaVu fonts, so the shots show DejaVu Sans, Serif and Mono, which are the last fallbacks. On Windows and macOS the serif is Sitka/Cambria and Charter, narrower and lighter than DejaVu Serif. I could not capture those.
- **Browsers and devices:** Chromium only (Playwright headless). There was no Firefox or Safari, no real phone, no screen reader and no keyboard-only pass. The dark theme was captured with `?theme=dark` and also emulated with `prefers-color-scheme: dark`.
- **The mock-ups are static and the data is invented.** The project switcher, menu button, filters and switch are drawn, not wired. The phone menu (the rail as a drawer) is not drawn.
- **Fixed in the mock-up only:** the current tab is scrolled into view by a three-line inline script. The product would do the same in `app.js`.
- **Only six pages are drawn.** The rest (Reviews, Records, Rooms, Lanes, Settings, Set up, How) would take the same header, table and state patterns. They are not mocked.

## 11. Build slices

**Superseded by the build plan** (11-06, base .523's c6c8eb3e). That plan uses the frozen names of §8b. It also runs the AA re-measure as a repo test behind .523's `ORCHESTRA_WEB_TEST_BROWSER` switch, so the "no browser harness" remark below is out of date. The table below is kept for its file:line list.

These follow the coordinator's order. Each one needs .523's names landed first (§8b). Line numbers are at b08ca8d4.
- **Structure is .522's:** the rail, tabs, switcher, the Agents table and the Project › People/Agents sections are built by .522. These slices only style what .522 renders and move nothing.
- **What every slice's proof includes:** the same **AA re-measure on built pages**, and `python3 -m unittest` plus the `node` shim tests.
  - **The re-measure:** a Playwright script, run in the lane through `heavy.sh` on `web/prototype.html` (mock data). It visits sign-in, My work, Tasks, a task, Agents and Project › People/Agents at 1280 and 375 px, in light and dark (both `data-theme` and emulated `prefers-color-scheme`).
  - **For every visible text node** it compares the computed `color` with the first opaque background among its ancestors, and fails below 4.5:1 (3:1 for text ≥24 px, or ≥18.66 px bold).
  - **For every input, select and button** it fails an edge below 3:1.
  - It prints a table like §9.
  - **Why it is not in CI:** the repo has no browser test harness at b08ca8d4 (web tests run on `tests/web_dom_shim.mjs`). So the CI half is the static token test in (a), and the browser half is the lane script, with its output pasted into the slice's note.

| slice | files in web/ (file:line at b08ca8d4) | the test that proves it |
|---|---|---|
| **(a) Token values** | `css/app.css:2-36` (light), `:37-54` (dark, both copies); `:26-28` fonts; `:67` h1 uses `--font-display`; `:143`, `:157`, `:171-173` borders → `--field-line`; `:204` add `.chip.info` next to `.chip.accent`; `js/ui.js:21` `'awaiting-review'` → `warn`, `:32` `in_progress` → `info` | A new `tests/test_web_look_tokens.py` (static, runs in CI) parses both token blocks of app.css and asserts every value in §8b, and that the 29 pairs of §9 meet 4.5:1 or 3:1 in light and dark. `test_rooms_http.py:274` (the "Awaiting review" text) still holds. Then the browser AA re-measure. |
| **(b) Page header** | `js/ui.js:64-72` `pageHead` gains an optional `key: { num, label }`; `css/app.css:107-110` `.page-head` becomes the three-column grid (§6). Callers: `js/views/work.js:61` (My work: the count of rows waiting on you), `js/views/project.js:610` (Tasks: h1 "Tasks"; the key from `page.total`, `project.js:581`; the summary strip at `:600-605` gives way to the key), `js/views/task.js:356` (time waiting, Approve / Send back), `js/views/agents.js:509` (or .522's Agents page), and .522's Project › People/Agents | A new `tests/web_page_head_key.mjs` on the DOM shim: `pageHead` with `key` renders one `h1#page-title`, one `.key-num`, one `.key-label` and at most one `.btn.primary`; without `key` the output is unchanged; each of the six views renders one h1 and a key. `web_how_screen.mjs:41` (first H1) and `web_password_forms.mjs:70` (`#page-title`) still hold. Then the AA re-measure (the key number is large text). **Gap:** "4 wait on you" on Tasks needs a count the list read does not return today. Until the server gives one, the key shows only the total. |
| **(c) Tables and lists** | `css/app.css:181-191` (table, th, td), `:193-206` chips, `:374-380` who-acts, `:418` `.count`, `:425-440` the task-list widths, `:444-456` `stack-narrow`; `js/views/work.js:8-33` `workTable` (a `mine` row class is not needed on My work), `js/views/project.js:547-573` (a `mine` class when who-acts is you; `num` on Updated); `js/views/agents.js:216` `.agent-grid` (style only) | **Update** `web_task_title_clamp.mjs:60-62`, which pins the 900/1000 px min-widths that .523 drops. **Keep** `test_site_defects_504.py:119-121`. **Add** .523's bounding-box test (W §5 l.110-116): in Chromium at 1280, 900 and 375 px, no state chip extends past its cell and no page scrolls sideways at 375 px (the fresh mock-ups measured no sideways overflow on any of their 12 page-and-width captures; chip bounds were not measured there). Then the AA re-measure (chips, greyed rows, hover). |
| **(d) Sign-in** | `js/views/auth.js:5-48` (`login`: split layout, brand side, "Have a reset code?" by Password), `:50-81` (`redeem`: the same frame); `js/dom.js:42-56` `brandMark` (the new three-bar tile; also drawn in the rail by `js/app.js:137`, which is styled, not moved); `css/app.css:86-87` `.brand`, `:263-266` `.auth` | `web_password_forms.mjs` (`auth.login`/`redeem` at :72-131) and `test_http_web.py:2489-2500` still pass: field ids `username`/`password` and the `focusFirstEmpty` call (`auth.js:46`) are unchanged. A new shim check: `login` renders one `h1`, a `form` with both fields, the brand with an `aria-hidden` mark of 3 `rect`s, and no external `href`/`src`. AA re-measure on the sign-in page at 1280 and 375 px, light and dark (the brand side is hidden at ≤760 px). |
| **(e) Empty, loading, error** | `js/ui.js:74-76` `empty` (icon, title, sentence, optional action), `:78-80` `loading` (skeleton plus hidden label, `role="status"`), `:114-120` `errorState` (inline, `role="alert"`, request id, Try again); `css/app.css:251-253`, `:415` `.empty-line` (kept); `js/views/agents.js:199` (the 6 rem "Loading your agents…" box becomes the skeleton). Callers such as `work.js:7` and `project.js:578` keep their calls. | `web_slice5_reviews_page.mjs:35` and `web_slice5_agents_work_page.mjs:17` (the `.empty-line` text) still hold. A new shim test: `empty()` returns `.state` with its title and body; `loading()` returns `role=status` with a hidden label; `errorState()` returns `role=alert` with the request id and a Try again that calls `retry`. A static check that the shimmer stops under `prefers-reduced-motion`. AA re-measure, including `ink-2` on `crit-soft`. |
