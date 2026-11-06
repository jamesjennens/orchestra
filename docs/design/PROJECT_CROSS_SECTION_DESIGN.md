# The selected project as a cross-section of the website (design note, kittrial-5bb.522)

Base: main b08ca8d4. All file:line references are at that commit. This note proposes no code; slices
follow VERIFIED.

**The ask** (James, 11-05, refined 11-06):
- Choosing a project filters the site to it until another project is chosen.
- The project tab always shows the last selected project.
- People and Agents each get their own tab showing the whole universe they may see, with filters.
- The project tab shows that project's own people and agents, with an "add" that picks from the whole
  universe.

**The coordinator's additions:**
- **(a)** A role or grant is given in ONE place: the project's People or Agents section.
- **(b)** Creating a person or agent happens on its own tab. An optional "add to project P as ROLE" calls the
  same route.
- **(c)** The selected project shows on every page it filters. A view filter is never a permission.

## 1. Today's menus and pages

There is one left rail, built by `buildShell` (web/js/app.js:121-161).
- **The selected project** is whatever the URL names: `projectOf(route)` reads `^/p/{pid}` (web/js/routes.js:69-73).
- **The switcher** is a `<select id="project-switch">` (app.js:129-132). Its selected value is derived from the URL and stored nowhere (app.js:131).
- **When there is no project in the URL,** no project sections show (app.js:145).
- **Nothing remembers a project.** No localStorage key exists. The only sessionStorage use is the agent wizard, `orchestra.agentSetup.v1` (web/js/views/agents.js:537, :720-744), plus lifecycle retry keys in web/js/api.js. Sign-in always goes to `/` or `/welcome` (app.js:62-66).

| Rail entry / route | Page | Class | file:line |
|---|---|---|---|
| My work `/` | `work.home`, GET /v1/me/work | cross-project | app.js:139; routes.js:11; work.js:37-39 |
| `/welcome` | first-run page | installation-wide (self) | routes.js:12; work.js:98 |
| My agents `/agents` | the viewer's own agents (all, for a superuser) | cross-project | app.js:140; agents.js:195; http_auth.py:5576-5583 |
| `/setup` | agent set-up wizard | cross-project (one agent) | routes.js:15; agents.js:1312 |
| All projects `/projects` | project list, create or register | installation-wide (as the viewer sees it) | app.js:141; work.js:219 |
| Tasks `/p/{pid}` | `project.overview` | project-specific | app.js:86; project.js:475 |
| Reviews, Releases, Requirements, Records, Decisions, Rooms, Lanes, Orchestra feedback, How work is found | one page each | project-specific | app.js:87-99; routes.js:17-42 |
| Program `/p/{pid}/program` | the PM view, only on a project with an accepted program side | project-specific (the program project) | app.js:95; program.js:10-12, :159-166 |
| Members & settings `/p/{pid}/settings` | members, agents in this project, credentials, audit, guidance, coordination, delegations, claiming, agent approval | project-specific | app.js:97; project.js:821-1000 |
| Set up `/p/{pid}/setup` | the owner's checklist | project-specific | app.js:98; setup.js:130 |
| `/p/{pid}/handover` | the coordinator walkthrough (.442-.444), linked from Members & settings only | project-specific | routes.js:25; handover.js:37; project.js:995 |
| `/p/{pid}/notes` | notes between coordinators, linked from settings only | project-specific | routes.js:26; project.js:995 |
| People & access `/admin/users` | accounts: create, reset, disable, project-create grant | installation-wide, superuser only | app.js:146-148; admin.js:5-78 |
| Account `/account` | own password | installation-wide (self) | app.js:153; admin.js:117 |

**Who may read the lists the new tabs need.** This is unchanged and decides what each tab can show.
- **Accounts:** GET /v1/accounts requires CAP_ACCOUNTS_ADMIN, so superusers only (http_service.py:8266-8268).
- **Agents:** GET /v1/agents returns the viewer's own agents, or every agent for a superuser (http_auth.py:5576-5583).
- **A project's agents:** GET /v1/projects/{pid}/agents requires CAP_PROJECT_ADMIN, so owners and superusers (http_service.py:10943-10945).
- **A project's members:** GET /v1/projects/{pid}/members requires CAP_READ, so any member (http_service.py:9678-9680).
- **Adding a member:** the add flow resolves an exact username with GET /v1/accounts/lookup for the project being added to (http_service.py:8289-8296; project.js:870-882).
- **Roles:** set with PUT /v1/projects/{pid}/members/{uid} (project.js:847). The roles are viewer, contributor, coordinator and owner (project.js:35).
- **Coordinator and release grants:** PUT/DELETE /v1/projects/{pid}/agents/{aid}/coordinator and …/release (project.js:927-934).

## 2. The selected project as a cross-section

**Where it is chosen.** It is chosen in three places:
- the rail's switcher, as today (app.js:129-132);
- a project card on Projects;
- any `/p/{pid}/…` link.

Opening a project URL selects that project. There is no separate "select" step.

**How it persists: the last selected project, per person.** Two layers:
- **The URL** is the truth for the page in front of you. A `/p/{pid}` route always shows `pid`, whatever is remembered.
- **The remembered project** is written each time a project route renders. It is read only when a page without a project in its URL needs one: the Project tab, and a filter's default (below).
  - **Slice 1 store:** `localStorage['orchestra.lastProject.' + me.id]`. It is keyed by the person, so two people on one browser do not share it. It is cleared on sign-out next to the wizard storage (app.js:67-69, `agents.clearWizardStorage`).
  - **A per-device choice:** this store remembers the project per browser, not across devices. Q1 proposes a server-side preference as a later slice.
- **A remembered project the viewer can no longer read** (left, archived, removed) is dropped silently when `ctx.projects` (GET /v1/projects, app.js:58-60) does not list it. The Project tab then asks the person to choose.

**What a project-specific page shows under it.** Nothing changes for those pages. They already take their project from the URL, and each reads its own project-scoped route. The cross-section is the rail and the tabs, not the pages.

**How a page shows it is filtered (c).** Every page whose content is narrowed by the selection carries one line under its title:

```
Showing: Alpha  ·  change   ·  show all projects
```

- **Project pages:** the breadcrumb already names the project (project.js:42-44). They gain no extra line, because they are not filtered, they are scoped.
- **People, Agents and My work:** these are filtered by the selection. They show the line, and "show all projects" clears the filter for that view.

**Agents not in the project (James: "optionally include agents that are not assigned").**
- **Default:** the project's Agents section lists only agents with the project.
- **The option:** a toggle, "Include my agents not in this project", adds the viewer's other agents, each greyed with an "Add to this project" button. That button uses the existing PATCH /v1/agents/{aid} with `projects` (agents.js:401), the same route My agents uses.
- **Someone else's agents** are never listed: the viewer cannot read them (http_auth.py:5576-5583).

## 3. Tabs

The rail becomes:

```
┌──────────────────────────┐
│ Orchestra                │
│  My work                 │  cross-project; filter = selected project (optional)
│  People                  │  installation universe, filters
│  Agents                  │  agents I may see, filters
│  Project: [Alpha ▾]      │  the selected project's own view
│     Tasks · Reviews …    │  (today's project sections, unchanged)
│  Programs                │  only when I coordinate or own a program project
│ ──────────────────────── │
│  Administration          │  superuser: installation pages
│  Account · Sign out      │
└──────────────────────────┘
```

### 3.1 People

```
People                                   [ New person ]  (superuser)
Showing: Alpha · change · show all projects
Filter: Project [Alpha ▾]  Role [any ▾]  Active [yes ▾]   Search [____]
┌──────────────┬──────────┬───────────────────────────┬────────┐
│ Name         │ Username │ Projects (role)           │ Active │
│ Cora Lee     │ cora     │ Alpha (coordinator), Beta │ yes    │
│ Carl Novak   │ carl     │ Alpha (contributor)       │ yes    │
└──────────────┴──────────┴───────────────────────────┴────────┘
```

**The universe a viewer may see** (the routes of §1, unchanged):
- **A superuser:** every account (GET /v1/accounts), with their projects joined from each project's members.
- **Anyone else:** the members of the projects they can read, which is the union of GET /v1/projects/{pid}/members over `ctx.projects`. This is the only universe their existing rights give. Q2 asks whether that needs a read of its own, since a page per project is N calls.

**On this tab:**
- **"New person"** is the superuser's existing create-account form (admin.js:102). It moves here from People & access (b). Its optional "Add to project P as ROLE" then calls PUT /v1/projects/{P}/members/{uid}, the same route as the Project tab. It shows only to someone who may do both.
- **Roles are not set on this tab (a).** The Projects column links to that project's People section, where the role is set.
- **Account actions** (reset, disable, the project-create grant) stay where they are, under Administration (§3.5).

### 3.2 Agents

```
Agents                                    [ New agent ]
Showing: Alpha · change · show all projects
Filter: Project [Alpha ▾]  Owner [me ▾]  Role here [any ▾]  Online [any ▾]
┌──────────┬────────┬──────────────────┬──────────────────────┬──────────┐
│ Agent    │ Owner  │ Projects         │ In Alpha             │ Last seen│
│ Heron    │ cora   │ Alpha, Beta      │ coordinator (grant)  │ 2 min    │
│ Egret    │ cora   │ Beta             │ —  [Add to Alpha]    │ 1 h      │
└──────────┴────────┴──────────────────┴──────────────────────┴──────────┘
```

- **The universe:** My agents' list (GET /v1/agents: one's own, all for a superuser), unchanged. Today's My agents page *is* this tab, renamed, with the filters added (agents.js:195-213).
- **Owner and grant rows:** for a project the viewer owns, the "In Alpha" column also shows other people's agents in that project, from GET /v1/projects/{pid}/agents (owners only, http_service.py:10943). Those rows are read-only here. Their grant is given on the Project tab (a).
- **"New agent"** is today's create (agents.js:498) and set-up wizard (agents.js:1312). Its optional "Add to project P", and .521's "coordinator of P", call the same PATCH /v1/agents/{aid} and PUT …/coordinator routes as the Project tab. They show only to someone who may grant them, read with the same capability check the Members page uses (.521's own rule).
- **Online:** from the agent's `last_seen_at`, stamped on use (http_auth.py:2166) and in the agent view (:3657), already in the agent view.

### 3.3 Project (the last selected project's own view)

```
Project: Alpha ▾                                         (archived banner etc. as today)
Tasks · Reviews · Releases · Requirements · Records · Rooms · Lanes · Program* · People · Agents · Settings · Set up* · How
                                                                        (* as today's conditions)
People in Alpha                        [ Add person ]   ← picks from the People universe
┌──────────┬───────────────┬─────────┐
│ Cora Lee │ coordinator ▾ │ Remove  │   ← PUT/DELETE /v1/projects/{pid}/members/{uid}
└──────────┴───────────────┴─────────┘
Agents in Alpha                        [ Add agent ]  [ ] include my agents not in Alpha
┌──────────┬──────┬──────────────────────┬─────────┐
│ Heron    │ cora │ Coordinator: Remove  │ Release │   ← PUT/DELETE …/agents/{aid}/coordinator, …/release
└──────────┴──────┴──────────────────────┴─────────┘
```

- **The Project tab** is `/p/{remembered}`. A rail click on "Project" with nothing remembered shows the project picker, which is today's `/projects` cards.
- **Members & settings is split in two:**
  - **People** (`/p/{pid}/people`) and **Agents** (`/p/{pid}/agents`) take the members panel (project.js:832-882) and the "Agents in this project" panel (project.js:888-934) as they are.
  - **Settings** keeps claiming, agent approval, credentials, audit, guidance, coordination, delegations, and the links to the hand-over walkthrough and notes (project.js:990-1000).
  - **Old links:** the old `/p/{pid}/settings` route stays and opens Settings, so old links work.
- **"Add person":** today's username lookup (GET /v1/accounts/lookup, project.js:878) plus role, then PUT member. A superuser may also pick from the People list. Everyone else adds by exact username, as today, because they cannot list accounts.
- **"Add agent":** for the viewer's own agents, PATCH /v1/agents/{aid} `projects` (agents.js:401). Another person's agent joins a project only through its owner, as today (`_agent_projects`, http_auth.py:3314). The section says so: "Ask cora to add Egret to this project from Agents."
- **This is the one place a role or grant is given (a).** The wizard's "coordinator of P" (.521) and the create flows' "add to P" call the very same routes, so the two can never diverge. A named test pins it (§7).

### 3.4 Programs (the PM view)

- **The view itself is unchanged.** The program page stays a page of the program project (`/p/{program}/program`, program.js:159). Its menu condition stays the server's cached flag (app.js:95; PROGRAM_DESIGN.md §5 "The menu").
- **A rail entry is added.** "Programs" lists the program projects the viewer can read, from the `program` flag already on each `ctx.projects` item (program.js:10-12). One program opens its page directly.
- **Choosing an area** from the program page selects that area as the project (a `/p/{area}/…` link), like any project link.

### 3.5 Administration (installation-wide, superuser)

- **What stays here:** account actions (reset, disable, the project-create grant: admin.js:13-78) and the operator-facing pages later tasks add.
- **What moves:** listing and creating people moves to People (3.1), because "People & access" today does both. Administration then holds only what changes an account rather than a project.

### 3.6 The coordinator onboarding walkthrough (.441-.444)

- **The page stays project-specific** (`/p/{pid}/handover`, handover.js:37). It is linked from the Project tab's People and Agents sections as well as Settings.
- **Its fix links point at the new homes.**
  - O1, "make the person a coordinator", goes to Project › People.
  - O2/O3, "agent has the project" and "grant the role", go to Project › Agents.
  - C1, "set up the folder", goes to the wizard.
- **Each step's check is unchanged** (COORDINATOR_ONBOARDING_DESIGN.md §4). Only the fix links change. Today they go to Members & settings and My agents (handover.js:15-18).

## 4. The Projects tab

"Project" in the rail is the last selected project's own view, `/p/{remembered}` (§2):
- **When nothing is remembered,** the rail shows the switcher as today, with "Choose a project…" (app.js:130).
- **A person with one readable project** has it selected on first sign-in, so the Project tab is never empty for them.
- **All projects** (the list, create and register: work.js:219) moves under the switcher as "All projects…", its last option, and keeps its route `/projects`.

## 5. Deep links, Back, several tabs, one project

- **Deep links:** every project page keeps its `/p/{pid}/…` hash route (routes.js:16-42). Opening one selects that project and updates the remembered project.
  - **People and Agents** carry their filter in the hash query, e.g. `#/people?project=alpha&role=coordinator`. The router already passes `?…` to views (app.js:171-174), so a filtered view can be shared and bookmarked.
- **Back and Forward:** these work as today, through `hashchange` (app.js:205-213). There is no popstate handler. A filter change is a new hash, so Back undoes it. Updating the remembered project never navigates, so Back is never hijacked.
- **Sign-in to a deep link (Q4):** today it lands on `/` or `/welcome` (app.js:62-66), never on the requested page. A small fix in slice 1 brings the person back to it.
- **Several browser tabs on different projects:** each tab's page follows its own URL. The remembered project is "the last one any tab rendered". Two rules keep that harmless:
  - a page never changes because another tab changed the remembered project, since the store is only read at navigation and is not watched;
  - a filtered list says which project it shows (§2), so a stale default is visible, not silent.
- **A person with one project:** the switcher, filters and "show all" still show, with one choice (Q5).

## 6. What must not change

- **Authority is unchanged.** Every route, its capability check and its answer stay exactly as they are. The selection lives in the browser (slice 1) and is read by no server route. A server preference (Q1) would be read by no authority decision either.
- **A view filter is never a permission.** People, Agents and My work narrow what they show. They never decide what the viewer may do. An action on a row calls its route, which checks as today.
- **Nothing hidden by the filter is treated as absent.**
  - A filtered list says it is filtered ("Showing: Alpha") and offers "show all projects".
  - A count on a filtered page names its filter: "3 agents in Alpha", not "3 agents".
  - My work's attention counts stay whole by default (Q3).
  - Nothing on the server reads the selection, so no answer can depend on it.
- **Routes are kept.** Every existing route keeps working: `/agents` opens Agents, `/admin/users` opens Administration, `/p/{pid}/settings` opens Settings.
- **Visibility is unchanged:** other people's agents, `working_directory` (owner or superuser only, `agent_view` http_auth.py:3639-3642) and accounts stay exactly as visible as today.

## 7. Slices, smallest first, each shippable alone

**Base: kittrial-5bb.502.** This note is the delta to .502's site review (lane report of 11-01; its §3 "Proposed
structure" and §5 "Ranked changes"). The coordinator's 02:30Z correction and 03:00Z VERIFIED set slice 1 to
.502's change 1 plus 7 (the orthogonal switch).

**What .504 already fixed:**
- .502 §1.2 measured the switcher always landing on Tasks (app.js:92 at that commit). .504 had already removed
  that always-Tasks behaviour before slice 1 began; slice 1 does not touch it.
- .504 (187b170c, 995e5b31) made it keep the section: `switchProject` (app.js:105-119) with `switchTarget`
  (routes.js:78-92) and the toast `fallbackMessage` (routes.js:96-100).
- Pins: tests/test_site_defects_504.py:17-43 and :70-84.

**What is still open from .502 §3 "What survives a project switch":**
- **The query is dropped.** `switchTarget` cuts it (`route.split('?')[0]`, routes.js:82 at b08ca8d4).
- **No project is remembered.** The switcher reads "Choose a project…" on every page without a project (app.js:130).
- **.502 §1.9:** the Tasks filters live in a closure, `state` (project.js:484 at b08ca8d4), and are not in the URL. Reviews has no
  filter at all (project.js:634-688 at b08ca8d4).

**Slice 1, as VERIFIED.** It fixes the rest of .502 §1.2 (what the switch loses besides the section: the query and
the project itself; the always-Tasks landing is .504's, above) and §1.9 for Tasks and Reviews:
- **(a) The query survives a switch.** On `#/p/A/reviews?review=changes-requested`, choosing B opens
  `#/p/B/reviews?review=changes-requested`.
  - **Only view filters cross:** `status`, `q`, `review`, `requirements`.
  - **Only values that mean the same in every project cross.** `status` crosses as `active`, `closed` or `all`;
    `review` as one of the review-state words (`awaiting-review`, `legacy-review-ready`, `changes-requested`,
    `approved`, `awaiting-integration`, `error`, `withdrawn`, `superseded`, `none`); `requirements` as `1`. Any
    other value is dropped (a task id typed into `status` does not cross).
  - **The `q` pattern (one):** a search crosses unless the value contains the shape of an id, that is a letter or
    digit, one or more of `-`, `_` or `.`, then a letter or digit (`/[A-Za-z0-9][-_.]+[A-Za-z0-9]/`, routes.js
    `ID_SHAPED`), tested once on the whole value: no splitting and no trimming, so an id next to any character
    (`` `X` ``, `#X`, `<X>`, `id=X`, `X/`, a pasted task link, `key@3`, smart quotes, `X…`, `task__x`) is found,
    and the test is linear. It covers `task_18cb2f`, `alpha-1`, `alpha-7k2`, `room_7f3a`, `r-9d4487e9c7da`,
    `REQ-7`, `kittrial-5bb.522`. A search for an id names one record of one project, so it stays behind. Plain
    words cross. Any hyphenated or dotted word (`set-up`, `e.g.`, `v1.2`) has the shape too and stops crossing;
    that is accepted.
  - **A search that stays behind is said so:** the toast adds "The search was not carried over, because it names
    something in A." (A is the project left), alone or after a fallback sentence.
  - **Records' `state`** is not carried: no page writes it, and only keys a page writes are carried.
  - **A key that names a record of A** (`?proposal=<key>` on Reviews, project.js:638) or a mode of one page is
    dropped, because B has no such record.
- **(b) Fallback to Tasks.** If the section is not available in B for this person (Set up for a non-owner, Program
  where B has none), B's Tasks list opens and the toast says so once. This is as today; the filters still apply to
  Tasks. Handover and Notes are not rail entries and keep their section in any project the person can open.
  Handover opens for every member; Notes can answer "No access" to a viewer of B (the page says so; no toast).
- **(c) A record page of A** opens B's matching list where B serves it, with nothing of the page's query, and the
  toast gives the true reason (one sentence each, routes.js `fallbackMessage`):
  - a task page (`#/p/A/t/ID`) opens B's Tasks: "Task pages belong to one project, so B opened on Tasks.";
  - `/new` opens B's Tasks: "A new task is written in one project, so B opened on Tasks.";
  - a room page opens B's Rooms, a requirement page its Requirements, a record page (reference or capability
    included) its Records, a decision page its Decisions: "A room belongs to one project, so B opened on its Rooms
    list." (likewise requirement, record, decision);
  - where B does not serve that list (Decisions on a server without them), B's Tasks opens: "B has no Decisions
    page, so it opened on Tasks.";
  - a section B lacks (Set up, Program): "B has no Set up page, so it opened on Tasks." (filters kept);
  - an unknown section of A (a hand-written URL) and any other page of A (a design or trace page): "That page belongs to one project, so B opened on Tasks."
- **(d) The project is remembered.** The last project a person opened is kept in their browser as
  `orchestra.lastProject.<user id>` and cleared on sign-out and when the session is lost (a 401).
  - On a page without a project (My work, My agents, Projects), the rail shows that project in the switcher, with
    its sections, instead of "Choose a project…" (.502 §3).
  - A remembered project that a SUCCESSFUL `GET /v1/projects` no longer lists is forgotten. A failed read says
    nothing about access: the key is kept and nothing is shown from it until a read succeeds.
  - It is written after the project page's view answered, so a session that has ended writes nothing.
  - The URL always wins on a project page.
- **(e) Return to a deep link after sign-in** (Q4), for the person it was opened for: with nobody signed in when it
  was opened, whoever signs in; after a sign-out or a lost session, only the person who was signed in (the next
  person, including after Back on the login page, lands on `#/`).
- **(f) .502 §1.9 for Tasks and Reviews:**
  - **A write belongs to the render that asked for it.** The search box writes after 250 ms; a view passes the
    render token it took when it started (`ctx.setQuery(params, ctx.currentView())`), and a write from a render
    that is no longer current, or while another page's navigation is pending, writes nothing. Sign-out and a lost
    session end the render too.
  - **Tasks:** the filters (status, search, review state, show requirements) are read from the query on load. Each
    change writes them back with `history.replaceState`, so a filtered list is shareable and reload-safe and
    survives the switch.
  - **Reviews:** gains a `review` filter (one group, or all), read and written the same way.
  - **Back and Forward:** `replaceState` adds no history entry, so Back still goes to the previous page, not the
    previous filter. This supersedes §5's "a filter change is a new hash".
- **Tests:** a named test per rule (a)-(f). The mutants for (a)-(c) revert to app.js:92's old behaviour (always
  `/p/{B}`), drop the query, and carry every key.
- **What it breaks:** nothing pinned.
  - test_site_defects_504.py:26 asserts `?x=1` is dropped, and `x` is not a filter, so it stays true.
  - Old links work: a URL without a query opens the default filters, exactly as today.

The slices below are renumbered after it: the earlier slice 1 is now part of it, and slices 2-6 are unchanged.

Every slice:
- uses Node DOM tests (tests/web_dom_shim.mjs, run by `run_node_module`, tests/test_http_web.py:48);
- gets a named test per rule, each shown failing by a mutant;
- gets one real-browser check per slice. Playwright is not in the repo, so this is the lane's own Chromium capture against a scratch service, as for kittrial-5bb.502/.504: screenshots of each wireframe state at 1280 and 375 px.

1. **Remember the project** (now inside slice 1, above).
   - **Change:**
     - store and read `orchestra.lastProject.<me.id>`;
     - clear it on sign-out;
     - the rail "Project" entry opens it;
     - pre-select the one project of a one-project person;
     - after sign-in, return to the requested deep link (Q4).
   - **Tests:**
     - a project route writes the key;
     - a non-project route does not;
     - sign-out clears it;
     - a project no longer listed is dropped;
     - two people on one browser get two keys;
     - Back after a project switch returns to the previous project page;
     - the store is never read on a `/p/{pid}` route (the URL wins).
   - **Mutants:** an unkeyed store; writing on every route; reading over the URL.
2. **Split Members & settings into People, Agents and Settings,** as project sections.
   - **Change:** panels moved as they are. `/p/{pid}/settings` keeps working. The handover fix links point at the new homes.
   - **Tests:** each panel renders on its section; the old route opens Settings; the handover links are right; no route call changes (the API stub records every call, and the set equals today's).
3. **The Agents tab:** My agents renamed, with filters (project, owner, role, online), the "include my agents not in this project" toggle and the "Showing" line.
   - **Tests:**
     - the toggle lists only the viewer's own agents;
     - "Add to Alpha" calls PATCH /v1/agents/{aid};
     - no other person's agent is listed except from the owner's project read;
     - the filter survives a reload through the hash.
4. **The People tab:**
   - **Superuser:** GET /v1/accounts joined with each project's members.
   - **Others:** the union of the members of their readable projects.
   - **"New person"** moves from People & access, and its optional "add to project" calls the same PUT member.
   - **Tests:**
     - a non-superuser never calls GET /v1/accounts;
     - the union is bounded and says when it was cut;
     - the add-to-project call equals the Project tab's;
     - the "Showing" line appears exactly when a filter is set.
5. **Programs in the rail,** and My work's optional project filter.
   - **Tests:**
     - the entry shows exactly for readable program projects;
     - My work's counts with no filter equal today's;
     - a filter is named on the page.
6. **(After Q1) A server-side "last project" preference,** a per-person field written by the browser and read by no route but GET /v1/me.

**One-route rule (a):** in slices 2-4 a named test asserts that every place that grants a role or coordinator grant calls the same API function (project.js's member PUT and agent coordinator PUT, and .521's wizard call). A mutant that gives the wizard its own call path fails it.

## 8. Open questions, each with a recommendation

- **Q1. Per browser or per person across devices?**
  - **Recommendation:** per browser now (slice 1, no server change). A server preference comes later (slice 6) only if James wants it.
  - **Why:** "per person" is met on one device, and the key is per person. Cross-device needs a new stored field, and nothing else needs one yet.
- **Q2. People for a non-superuser: the client-side union of per-project member reads, or a new read-only `GET /v1/me/people` that applies the same per-project checks?**
  - **Recommendation:** start with the union, bounded to 50 projects as My work is, and say when it was cut. Add the aggregate read if a pilot's project count makes the union slow, as .521's 30 s My agents load did.
  - **Why:** either way the universe is exactly what the viewer can read today.
- **Q3. Should My work filter by the selected project by default?**
  - **Recommendation:** no. My work stays whole, with an opt-in filter chip, because it is where attention across projects is noticed. Hiding a project's waiting work by default would treat filtered items as absent, against §6.
- **Q4. Return to a deep link after sign-in?**
  - **Recommendation:** yes, in slice 1. It is small (app.js:62-66), and deep links are only worth having if they survive sign-in.
- **Q5. A person with one project: hide the switcher?**
  - **Recommendation:** no. Keep it visible with its one choice, so the page looks the same when a second project arrives, and the "Project" label still says where you are.
- **Q6. "Programs" as a rail entry, or only as the program project's page?**
  - **Recommendation:** a rail entry, shown only when the viewer reads at least one program project. The PM view is priority 1, and today it is reachable only after selecting the program project itself.
