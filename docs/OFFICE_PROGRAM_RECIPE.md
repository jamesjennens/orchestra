# Office Program Recipe: One Program Manager over Several Area Coordinators

This one-page recipe configures an office Orchestra installation (`https://<server>`) where each direct report (`<person>`) owns an area project (`<area>`) with their own coordinator agent (`<agent>`), and one program manager (`<pm>`) watches progress across all areas and talks to each area's coordinator from the website. Every behaviour below is cited against current `main` (`file:line`).

## 1. Accounts, Projects, and Roles

Orchestra has four per-project roles — `viewer`, `contributor`, `coordinator`, and `owner` (`http_authority.py:386-398`) — and no organisation or manager/report hierarchy. Set up projects and memberships as follows:

1. **Create one area project per direct report (`<area>`) and one program project (`<program>`).**
   - On **All projects** (`#/projects`, `POST /v1/projects`, `http_service.py:8835-8851`, `web/js/routes.js:13`), each `<person>` creates (or has a superuser register) their own `<area>` project and holds `owner` there.
   - `<pm>` creates (or is made `owner` or `coordinator` of) the `<program>` project.
2. **Add `<pm>` to each `<area>` as `contributor` (not `viewer`).**
   - On `#/p/<area>/people` (**People**, `PUT /v1/projects/{pid}/members/{uid}` or **Invite someone or add an agent**, `web/js/views/project.js:1130-1152`, `web/js/views/invites.js:32,52`, `http_service.py:10131-10144`), `<person>` adds `<pm>` as `contributor`.
   - Why `contributor`: read access (`CAP_READ`) lets `<pm>` see the area on the Program page (`http_service.py:13578-13609`), and `contributor` or above (`MEMBER_NOTE_ROLES`) is required for `<pm>` to message `<area>`'s coordinators (`http_auth.py:3981`, `http_service.py:10899-10937`). A `viewer` can read but cannot send member notes.
   - Optional: add each `<person>` to `<program>` as `contributor` on `#/p/<program>/people` if they need to read program requirements or file suggestions (`docs/design/PROGRAM_DESIGN.md:146-148`).
3. **Give each `<person>`'s coordinator agent the coordinator grant in `<area>`.**
   - `<person>` creates their personal agent on **My agents** (`#/agents`, `POST /v1/agents`, `web/js/routes.js:14`, `http_service.py:11023`) and grants it `<area>` (an agent can only be granted projects its owner is a member of, `http_auth.py:3315-3352`).
   - On `#/p/<area>/agents` (**Agents**), `<person>` clicks **Make coordinator** (`PUT /v1/projects/{pid}/agents/{aid}/coordinator`, `web/js/views/project.js:1111,1155-1166`, `http_service.py:11458-11469`). The grant takes effect only while the agent is enabled, holds `<area>`, its owner is `coordinator` or `owner` there, and the issuer still holds project admin (`http_auth.py:4663-4716`, `http_authority.py:858-891`).

## 2. Link Each Area to the Program (Both Sides Owner-Accepted)

A program is an ordinary project linked to each `<area>` by two reference records, each backed by a native `decision` issue (`docs/design/PROGRAM_DESIGN.md:62-97`, `program_links.py:1-18`):

| Side | Project | Reference key | Exact two-line statement | Who accepts |
| --- | --- | --- | --- | --- |
| Program | `<program>` | `program.area.<area>` | `Area: <area>`<br>`State: in` | Person `owner` of `<program>` (or operator) |
| Area | `<area>` | `program.member-of` | `Program: <program>`<br>`State: in` | Person `owner` of `<area>` (or operator) |

Steps on each side (repeat for each `<area>`):

1. **Create a decision issue** in that project (on any task page `#/p/<pid>/t/<tid>`, use **Add a child task** with type `decision` and headings `## Decision`, `## Rationale`, `## Alternatives Considered`, `web/js/views/children.js:9-37`; note its `<decision-id>`).
2. **Propose the reference record** on **Records** (`#/p/<pid>/records`, **Propose a reference record**, `POST /v1/projects/{pid}/references`, `web/js/views/records.js:456-486`, `http_service.py:13467-13471`) with Authority type `decision`, Authority target `<decision-id>`, and the exact two-line statement above (`program_links.py:51-61`). To leave later, revise the statement to `State: left`.
3. **Accept the draft as a person owner** on `#/p/<pid>/records/references/<key>` (`POST /v1/projects/{pid}/references/{key}/accept`, `web/js/views/records.js:866`, `http_service.py:13561-13565`). Only a person `owner`'s own web session (or a host operator) can accept or retire a `program.` key; coordinators and agents are refused (`http_service.py:13239-13245`, `program_links.py:38-40,100-112`).
4. **Verify link state** via `GET /v1/projects/<program>/program-links` (`http_service.py:13726-13756`). A link is `linked` only when both sides' live owner acceptances count (`program_view.py:87-95`); otherwise it reads `one-sided`, `proposed`, or `left`.

## 3. What Each Person Sees and Cannot See

- **A program link grants nothing** (`docs/design/PROGRAM_DESIGN.md:139-165`, `program_links.py:16-18`). Every cross-project read re-checks the caller's live `CAP_READ` per project (`http_service.py:13611-13695`, `http_authority.py:1224-1237`).
- **`<pm>`** (owner/coordinator of `<program>`, `contributor` in each `<area>`): sees each linked readable `<area>`'s open tasks, coordinators, lane counts, and full lane rows (`all` lanes) on `#/p/<program>/program` — the `<area>` owner's acceptance of the link is the consent that widens `<pm>`'s view from `own` to `all` when `<pm>` coordinates `<program>` (`http_service.py:13578-13609`, `program_view.py:150-161`). If `<pm>` is removed from an `<area>` or loses their `<program>` coordinator role, the next read drops to `not visible to you` (no rows or counts) or narrows to `own` immediately (`http_service.py:13659-13672`, `web/js/views/program.js:23-27`).
- **Each `<person>`** (`owner` of their own `<area>`): sees everything in their own `<area>`, and sees **nothing** in another `<person>`'s `<area>` unless explicitly added there. On `#/p/<program>/program` (if a member of `<program>`), another unshared `<area>` appears only as its link state and `not visible to you`, with no counts or lane rows (`http_service.py:13615-13621`, `web/js/views/program.js:23-27,36-37`).
- **Agents**: each personal agent reaches only the projects in its live grant list, capped by its owner's live role; any other project returns `404 Project not found` (`http_authority.py:831-855,1219-1237`).

## 4. Approvals by Coordinator Agents (`Settings`)

On `#/p/<area>/settings` (**Settings** → **Approvals by coordinator agents**, `PUT /v1/projects/{pid}/agent-approval`, `web/js/views/project.js:919-954,1244`, `http_service.py:9728-9748`, `http_auth.py:4729-4750`), only a project `owner` or superuser by web session may choose:

- **`by-agent` (default):** `<person>`'s coordinator agent may approve work delivered by another personal agent of `<person>` (or a sole-held worker credential name), never its own work or `<person>`'s bare account id (`http_authority.py:579-586,736-785`). This lets one `<person>` run a coordinator agent and separate worker agents in `<area>`.
- **`by-person` (strict):** all agents of `<person>` count as one party, so `<person>`'s coordinator agent never approves work by `<person>` or any other agent of `<person>` (`web/js/views/project.js:940-942`).

## 5. Watching Progress and Talking to Each Coordinator

- **Program page (`#/p/<program>/program`):** appears in the project rail once `<program>` has an owner-accepted `program.area.*` record (`web/js/app.js:179-180`, `web/js/views/program.js:9-11`, `program_view.py:103-108`). Shows coverage, each area's open tasks, coordinators, `waiting_too_long`, `expect_by_passed`, `your_move_idle`, and a collapsed **Lanes (`<n>`)** drill-down (holder, task, state, waiting on, expect-by, flags; up to 50 lanes) linking straight to `#/p/<area>/lanes` (`http_service.py:13611-13716`, `web/js/views/program.js:35-65`).
- **Area Lanes page (`#/p/<area>/lanes`):** `GET /v1/projects/{pid}/lanes` (`http_service.py:15818-15874`, `web/js/views/project.js:521-546`). Note: as a `contributor` (not `coordinator`) in `<area>`, `<pm>`'s direct Lanes view is scoped to `own` (`http_service.py:16154-16169`), whereas `#/p/<program>/program` shows all lanes of `<area>` over the live `linked` program link (`http_service.py:13578-13609`, `program_view.py:150-161`).
- **My work (`#/`, `GET /v1/me/work`):** `<pm>`'s assigned work and review attention across up to 50 memberships (`http_service.py:151,16171-16210`, `web/js/views/work.js:50-71`).
- **Messaging an area's coordinator (`#/p/<area>/notes`):** as a signed-in `contributor` (or above) in `<area>`, `<pm>` opens `#/p/<area>/notes` (reachable from **Settings** → **Notes between coordinators**, `web/js/views/project.js:1243`, or directly by URL) and uses **Write to this project's coordinators** (`POST /v1/projects/<area>/notes` with `to: "coordinators"`, `web/js/views/notes.js:89-101,124-162`, `http_service.py:10864-10938`).
  - Only `<pm>` (`GET /v1/projects/<area>/notes?box=mine`) and `<area>`'s coordinators (`box=to` / `box=sent`) read the thread (`http_service.py:10939-10982`).
  - An unsettled member note wakes `<area>`'s coordinator agents with a pointer only; coordinators reply with `to: "member"` and `reply_to` (or mark noted via `POST .../noted`) (`docs/design/MEMBER_NOTES_DESIGN.md:22-55`, `http_service.py:10912-11003`). Unread replies across projects appear on the Notes page via `GET /v1/me/notes` (`http_service.py:11005-11020`, `web/js/views/notes.js:148-158`). Limits: 10 member notes/hour per person & project, 60 coordinator replies/hour, 4000 characters plain text (`http_auth.py:3978-3984`).
  - A note is a request, not an instruction: the coordinator weighs it against its brief and `<person>`'s direction (`web/js/views/notes.js:94`, `web/js/agentSetup.js:602`).

## 6. What Does Not Exist Yet

- **No manager / direct-report concept:** searching the repository for `direct_report|direct-report|reports_to|line_manager|org_chart|subordinate|reporting_line` finds no matches; authority is strictly per-project membership plus explicit owner-accepted program links (`http_authority.py:386-398`, `program_links.py:1-18`).
- **No one-step program-linking UI:** linking an area still requires creating a `decision` issue and proposing/accepting the `program.area.<area>` and `program.member-of` reference records on `#/p/<pid>/records` (`web/js/views/records.js:456,866`).
- **No left-rail link to Notes:** `projectSections` omits `notes` (`web/js/app.js:169-196`); open `#/p/<area>/notes` from **Settings** (`web/js/views/project.js:1243`) or by direct URL.
- **No SSH enforcement of `by-agent` approval:** `PUT /v1/projects/{pid}/agent-approval` applies on the web route only (`capability-proposals/projects.agent-approval.json:18`).
