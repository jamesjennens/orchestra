# A project's coordinator writes and accepts its requirements

Slice 10 of `docs/WEB_COORDINATOR_DESIGN.md`; kittrial-5bb.278. James, 2026-10-14,
decided what this slice is for, in his own words:

> I want requirements to be updated by the coordinator through the web site without
> needing the operator step. Also want a coordinator to be able to do everything
> through the site. I am operator and coordinator on projects for now but I want to
> make other people coordinators of new projects.

Before this slice the site let only a project's OWNER write requirements. A
coordinator had to ask an owner, and the owner then ran a host command
(`admin.py requirement-apply`) about ten seconds per requirement. At the office one
coordinator populated about fifty requirements and the owner accepted them all by
hand afterwards.

## 1. Who may edit, and how that is judged

`http_authority.requirement_editor` is the ONE rule, used by the web service before
a route runs and again by the canonical endpoint at effect time. It returns the
account the record will name, the agent acting for it (if any), and the grant.

* A signed-in person whose current membership of the project is `owner` or
  `coordinator`. The membership is required, as it always has been on this path: a
  superuser who is not a member of the project is not its requirements editor.
* An agent credential whose agent holds the project's **coordinator grant**
  (`coordinator_grants[project][agent]`, slice 1b) while the credential's own human
  account still holds `coordinate` in that project. The grant is read live on every
  request, so removing it ends the reach on the next one.
* The agent's shape reads that human account's **membership** exactly as the person's
  shape above does (kittrial-5bb.318 item 3): `superuser` is accounts administration, not
  membership of a project, so the agent of a superuser who is not a member of the project
  is refused exactly as that superuser's own session is. Slice 10 left the choice open
  ("refuse both, or admit both and say why"); the pair is refused, because admitting the
  superuser family would widen this one rule past the membership it has always required.
  A superuser who owns or coordinates the project is a member, so their agent still edits.

Everything else is refused, and the sentence names the rule:

> Editing a project's requirements needs its owner or a coordinator: a signed-in
> member with the coordinator or owner role, or an agent holding the project's
> coordinator grant.

That covers a contributor, a viewer, a worker credential (which never holds
`coordinate`: no credential scope grants it), and an agent with no grant. A person
who is not a member of the project keeps the service's own 404, exactly as before,
so a refusal here never tells an outsider that the project exists.

The capability on every requirements route moved from `project.admin` to
`coordinate`. `coordinate` is held by an owner (its role includes every coordinator
capability), by a coordinator (its role, slice 1b) and by an agent only through the
grant (`GRANT_CAPABILITIES`). The endpoint action `owner-requirements` now names
`coordinate` in `SERVICE_ACTIONS`, and is the one service action a credential
descriptor may carry: `project_creation.CREDENTIAL_SERVICE_ACTIONS` is
`frozenset({'owner-requirements'})` and a test pins it to exactly that, so widening it
(for example to `set-onboarding`) fails a test rather than only surviving because the
other action's own decision still refuses (kittrial-5bb.318 item 2).

### Effect time, not only at the door

`requirement_http.owner_context` re-runs the same rule under the endpoint's
authority lock, before the native effect and on every replay. A coordinator demoted,
or a grant removed, between the web check and the write prevents every write. A batch
re-checks once per item, so a demotion in the middle stops the remaining items and
leaves the ones already accepted accepted.

## 2. No operator step on the web path

Nothing on this path reads the operator allowlist. The host commands
(`requirement-apply`, `requirement-backfill`, `requirement-reconcile`) keep their own
list, untouched; they remain the operator's, for repairs.

## 3. The party question, settled by the owner

Owner decision `01a13b6f-3007` (James, 2026-10-14, recorded on this task): *"Yes self
acceptance ok."* A coordinator MAY accept a requirement it proposed itself; the
slice-1a party rule does not apply to requirement acceptance. Every acceptance still
records **who proposed and who accepted**.

## 4. Who the record names, when an agent acts

Owner decision 11(ii): a granted agent's acceptance keeps naming the **owning human
account** as the accepting party; the agent and its grant are recorded in the service
audit.

That is exactly what the code does, and it needs no new record schema:

* the native decision's author, the acceptance evidence's `account_id`, and the
  decision's `owners`/`approvers` are the credential's own human account
  (`credential['user_id']`), so every existing receipt validator, evidence reader and
  older kit keeps working;
* the service audit entry for the write still carries `user_id` = that account and
  `credential_id` = the agent's credential, and gains `acting_agent` (the agent id)
  and `coordinator_grant` (`granted_by`, `granted_at`).

A worker credential cannot stand in an agent's place, and an agent cannot approve or
edit outside the project it was granted.

## 5. Batch acceptance

One new route, the only new one in this slice:

    POST /v1/projects/{project}/requirements/accept-batch
    body {"items": [{"id", "expected_revision", "expected_sha256"}, ...],
          "stop_on_error": false}
    header Idempotency-Key (required, as for every requirements write)

* 1 to 200 items. The whole body is validated **before any effect**: an empty list,
  more than 200, a duplicate id, a missing or malformed hash, an unknown field or a
  non-boolean `stop_on_error` is a 422 and nothing is accepted.
* Each item runs the SAME canonical adapter as a single acceptance, with its own
  operation id derived from the request's Idempotency-Key and the item id, so an
  exact replay of the batch accepts nothing twice, and a replay of one item inside a
  different batch is still a replay.
* Answer 200 with `accepted` (the per-item results), `refused`
  (`[{"id", "reason"}]`) and `summary` (`accepted`, `refused` counts). A refused item
  changes nothing and does not roll back the items accepted in the same request: a
  native acceptance cannot be undone (owner decision 11(iii)), so all-or-nothing
  would need a two-phase accept this kit does not have.
* `stop_on_error: true` stops at the first refusal; every item it never reached is
  listed in `refused` with *"Not attempted: stop_on_error stopped this batch after an
  earlier item was refused."* Nothing is dropped silently, and the page shows plainly
  which items were not accepted and why.
* An uncertain outcome (5xx) is NOT a per-item refusal: it stops the batch and its
  own answer stands, so a caller is never told "refused" about a write that may have
  landed.

## 6. What stays owner-only

* The requirements **governance-mode switch** (owner decision 11(i)): it changes the
  rules, not the text. A coordinator's PUT answers 403 with *"Switching requirements
  governance needs the signed-in project owner"*.
* Everything else that was the owner's stays the owner's, and this slice opens none
  of it: member management and owners, worker credentials and agents, the coordinator
  grant itself, archive/retire-project, the project audit read, the set-up page,
  deployment, and the host operator commands. `test_http_requirements_coordinator.py`
  asserts a coordinator is refused on each of those routes.
* The requirements path is the only place `_requirements_owner` and
  `requirement_http.owner_context` are called: `_requirements_write`,
  `requirements_recoveries`, and the read-side `_requirements_state_only` /
  `can_edit` helpers. No other route shares them.

## 7. How this meets kittrial-5bb.228 B2

Kittrial-5bb.228 B2 is named baselines, the reproducible Markdown export and
"changes since a baseline" (branch `contrib/kittrial-5bb.228-baselines-sol1`, Codex).
Slice 10 changes WHO may write and accept and adds batch acceptance; it does not
implement snapshots or export. The two touch the same requirements surface
(`http_service.py`'s requirement section, `requirement_http.py`,
`docs/REQUIREMENTS.md`, `web/js/views/owner_requirements.js`), so this delivery is
based on current `origin/main` and rebases onto B2 if B2 lands first.

## 8. Tests

`tests/test_http_requirements_coordinator.py`, against the shipped canonical adapter
with the native tracker modeled, and the real-bd module
`tests/test_requirement_owner_real_bd.py` for the pinned native path:

* **owner**: unchanged; no owner sees a difference (`can_edit` still true, every
  action still works);
* **coordinator person**: create, revise, accept (including self-acceptance),
  withdraw, supersede, finish/abandon a recovery, read the BRD, `can_edit` true,
  batch accept; the governance switch refused; member management, worker
  credentials, agents, the coordinators route, the audit read, archive and the
  set-up page all still refused;
* **granted agent**: the same through the routes and `http_client.py`; the record
  names its human account, the audit names the agent and the grant; `can_edit`
  false (an agent uses the routes, not the page);
* **refusals with zero writes**: contributor, worker credential, ungranted agent,
  and a coordinator of another project (the service's 404);
* **effect time**: a coordinator demotion, and a grant removal, between the check and
  the effect prevent the write; a demotion mid-batch stops the remaining items;
* **batch**: all accepted; one refused mid-batch leaves the others accepted with a
  reason each; duplicates, an empty list, 201 items, a bad hash, an unknown field
  and a non-boolean `stop_on_error` refused before any effect; `stop_on_error`
  names every item not attempted; an exact replay accepts nothing twice;
* **the page**: `tests/web_requirements_coordinator.mjs` drives the coordinator's
  requirements page over loopback HTTP and accepts two in one action.

Windows-safe: the new tests use no POSIX-only call (no `bash`, `fcntl`, shared
locks or sshd paths), so nothing here needs `unittest.skipIf(os.name == 'nt', ...)`.
