# A fresh look for the website (design note, kittrial-5bb.525)

**Status:** design only. No product code changes. The mock-ups are in `docs/design/web-fresh/`, sharing one stylesheet, `fresh.css`. There are 24 screenshots in `web-fresh/shots/`, plus a before/after pair of Tasks, and all of them are on `web-fresh/contact-sheet.html`.
**Base:** main at b08ca8d4.
- **Why today's token names:** .523's design note could not be read from this lane, so every token below uses today's names in `web/css/app.css:2-36`.
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

## 8. Tokens changed or added (today's names)

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
| `warn` / `warn-soft` | chip Awaiting review, Needs setup | 5.61 | 7.86 | 4.5 ✓ |
| `crit` / `crit-soft` | chip Changes requested | 5.65 | 6.95 | 4.5 ✓ |
| `ok` / `ground` | fact done | 5.97 | 9.89 | 4.5 ✓ |
| `warn` / `ground` | fact now | 5.86 | 10.18 | 4.5 ✓ |
| `ok` / `surface` | diff + lines | 6.51 | 9.18 | 4.5 ✓ |
| `crit` / `surface` | P0, diff − lines | 6.71 | 7.91 | 4.5 ✓ |
| `ink` / `crit-soft` | error title | 14.24 | 12.93 | 4.5 ✓ |
| `ink-2` / `crit-soft` | error text, request id | 9.55 | 9.20 | 4.5 ✓ |
| `field-line` / `surface` | input, select, button edges (1.4.11) | 3.56 | **3.37** | 3 ✓ |
| `focus` / `surface` | focus ring | 7.58 | 10.24 | 3 ✓ |
| `accent` / `surface` | current tab, "yours" bar, switch on | 7.58 | 7.86 | 3 ✓ |
| `crit` / `crit-soft` | error icon and rule | 5.65 | 6.95 | 3 ✓ |
| `ok` / `surface` | online dot | 6.51 | 9.18 | 3 ✓ |

- **Lowest text pair: 5.57:1.** That is `muted` on `surface-2` (light) and the Approved chip (light).
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

- **.523's note was unreadable** from this lane, so the mapping to its names is left to whoever has both.
- **Fonts in the screenshots:** the capture machine has only DejaVu fonts, so the shots show DejaVu Sans, Serif and Mono, which are the last fallbacks. On Windows and macOS the serif is Sitka/Cambria and Charter, narrower and lighter than DejaVu Serif. I could not capture those.
- **Browsers and devices:** Chromium only (Playwright headless). There was no Firefox or Safari, no real phone, no screen reader and no keyboard-only pass. The dark theme was captured with `?theme=dark` and also emulated with `prefers-color-scheme: dark`.
- **The mock-ups are static and the data is invented.** The project switcher, menu button, filters and switch are drawn, not wired. The phone menu (the rail as a drawer) is not drawn.
- **Fixed in the mock-up only:** the current tab is scrolled into view by a three-line inline script. The product would do the same in `app.js`.
- **Only six pages are drawn.** The rest (Reviews, Records, Rooms, Lanes, Settings, Set up, How) would take the same header, table and state patterns. They are not mocked.
