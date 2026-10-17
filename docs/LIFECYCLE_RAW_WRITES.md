# Lifecycle write routes

## Contributor boundary

Contributor raw `bd` requests cannot create, convert, edit or remove native
lifecycle state events, including through `close` and `reopen`, or add, remove,
replace or inherit their reserved labels.
Raw `update` cannot change **any** row to or from `event`, including an ordinary
task or event with no lifecycle content. Existing rows cannot be made ephemeral:
a plain native export (`bd export` without `--all`) omits ephemeral rows, so a
lifecycle row would vanish from any read that uses one - this kit's own exports
all pass `--all`, which keeps it (kittrial-5bb.260 F2). Ordinary event creation stays allowed;
closing a task with facts stays allowed and its facts stay readable.
Native status changes to `open`, `in_progress`, `blocked`, `deferred` and
`closed`, `--defer` and `--persistent` also keep the task's facts readable.
The pinned native tool rejects `tombstone` and `deleted` as status values;
the raw interface refuses unsupported `--delete` and deletion verbs before
writing. References supplied through `--parent`, `--deps` and `--waits-for`
are checked too: a new or existing row cannot be linked to a lifecycle event.
The namespace comprises the six fact dimensions, `lifecycle-scope`, `enabled`
and `live`. The endpoint judges old and final row content under the project
lock before invoking a native writer. Incomplete reads and ambiguous mutation
forms refuse the operation and name the dedicated lifecycle action.

Use `lifecycle.py record` for assertions and corrections. Ordinary descriptions
that discuss lifecycle states, ordinary events, and the legacy `review-ready`
and `reviewed` labels remain writable. The write boundary is always active on
the contributor route, independently of optional authority rules.

### Raw command surface

The endpoint allow-list is `list`, `show`, `ready`, `search`, `count`, `create`,
`update`, `close`, `reopen`, `comments`, `dep`, `state` and `lint`. This does
not authorize every spelling: existing read/write, identity, file, record-anchor
and command guards still apply; raw `delete`, `remove` and `tombstone` verbs
are outside this interface. Native lifecycle setting uses its dedicated action.
The lifecycle guard judges `create`, `update`, `close`, `reopen` and writing
`dep` forms: `add`, `remove`/`rm`, `relate`, `unrelate` and the parent
`dep ID --blocks ID` form, including transported edge files. Read-only
dependency forms retain their existing route. Every affected reference is
resolved by native `show`; substring spellings are not guessed from suffixes
of other rows named in the same call. Missing, ambiguous or incomplete reads
refuse before any native writer or operation journal.

### Children of scoped tasks

Use `create ... --parent TASK --no-inherit-labels` for a child of a task carrying
lifecycle labels, including `lifecycle-scope:HASH`. Without that flag the native
create would copy the parent's scope/facts, so the guard refuses and names the
flag. This intentionally keeps the existing explicit no-inheritance option;
there is no post-create label-stripping write that could fail after the child
was made, and no change to inheritance of other reserved namespaces. Explicit
ordinary labels on the child remain allowed.

## Writer inventory

| Writer or instruction | Route and disposition |
| --- | --- |
| `lifecycle.py::_apply_fact` | The only production literal native `set-state` writer; records scope, six dimensions, enabled and live facts, including intermediate values on correction. The optional lifecycle author rule checks this dedicated route before writing. |
| `lifecycle.py::apply_native`, `apply_release` | Dedicated lifecycle action and release groups call `_apply_fact`; unchanged from the current base. |
| `lifecycle.py::main` | Record, release query and release groups send `action='lifecycle'`; unchanged from the current base. |
| `endpoint.py::execute` | Dedicated lifecycle dispatch retains the current base author and independent approval rules. Contributor raw create, update, close, reopen and dependency writes pass the new guard before the native writer and its journal. Existing verb restrictions still apply. Listed operators bypass this added guard. |
| `client.py::request`, `main`; `worker.py::main` | Generic raw and structured transports accept caller arguments; callers that assert facts must select the dedicated lifecycle action. These transport implementations are unchanged from the current base. |
| `admin.py::run_bd`; direct native maintenance | Host operations retain their native route and existing authority. Shared host shell access is outside contributor confinement. |
| Native test fixtures | Deliberate direct native writes seed synthetic legacy or adversarial rows. They do not instruct contributors to assert facts through raw requests. |
| `docs/PILOT_FEEDBACK.md` | Names the dedicated record operation and the native implementation beneath it. |
| `docs/OPERATIONAL_WORKFLOW.md` | Documents scope/fact payloads, dedicated record/release commands and the raw namespace boundary. |
| `docs/WORKER_GUIDE.md`, `docs/WORKFLOW.md` | Direct workers to dedicated lifecycle recording and keep review-ready separate from facts. |
| `docs/CLI_CONTRACT.md` | Documents raw write refusal and ordinary-label compatibility. |
| `docs/WORKER_START.md`, `docs/HERMES_WORKERS.md`, `templates`, `start_here`, `README.md` | No raw lifecycle event or dimension-label instruction. Their existing structured workflow/report pointers remain valid. |
| Generated BRD and requirement publications | Describe independent evidence-scoped facts, not raw write instructions. They remain generated from canonical records. |

This inventory covers repository production Python callers and repository
documentation, prompts and templates. External caller arguments and installed
private project prompts are not an enumerable repository inventory.

## Compatibility and limits

This change reserves new contributor writes. It does not change legacy lifecycle
readers, dedicated lifecycle authorization, coordinator operations, operator
maintenance, backup format, or historical facts. Existing facts retain their
current interpretation when optional authority rules are off. Reader provenance
and historical repair require their own design and accepted policy integration.

The regression tests exercise final scalar flag values, hidden label aliases,
attachments, inheritance, replacement, multi-target writes, incomplete reads,
ordinary prose, close/reopen event protection, ordinary task status changes,
listed operator writes and genuine dedicated lifecycle writes.
The ordinary all-off worker flow covers claim, comment, status, label,
checkpoint, contribution and implemented/tested recording. A comparison beside
main retains exact return codes, stdout and stderr, normalizing only native
IDs, native timestamps, the activity cursor derived from them and server time.
Native checks use a disposable tracker; private review evidence retains its
synthetic export and the baseline/tip reader comparison.
