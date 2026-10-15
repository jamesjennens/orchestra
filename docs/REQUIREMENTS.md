# Working with requirements snapshots

The validator and publisher operate over explicit structured snapshots. Beads remains canonical for requirements, discussion and decisions. The [integration workflow](REQUIREMENTS_INTEGRATION.md) now resolves versioned revision comments from a native export, computes impact/reassessment views and gates worker launches on canonical plan acknowledgement. Arbitrary prose is never guessed into revision fields; server publication acknowledgement remains explicit.

The [BRD](BRD.md) and [source snapshot](requirements-baseline.json) are draft-0.1. They are not an accepted specification. The [contract](REQUIREMENTS_CONTRACT.md) records the implemented boundary and the disposition of the independent review.

## Editing a project's requirements in the web interface

In simple mode, a signed-in project owner opens **Requirements**, edits the text
and saves a draft. **Accept draft** or **Accept pending edit** records the owner's
decision for that exact revision.
Editing accepted text creates a new draft; the earlier accepted content remains
available to existing exact references. The business requirements document is a
read-only view of the records, including narrative, requirements
and decisions. Every project member, including viewers, can read it.

Newly provisioned projects start in simple mode. Existing projects with no
governance history remain governed. An owner can switch modes from the page;
changing modes preserves history and never accepts a draft automatically.
Credentials, agents and superusers without owner membership cannot use these
editing controls. The host's governed publication commands below retain their
existing operator authority.

In simple mode, contributors cannot replace a pending draft written by the
project owner, including an edit of accepted text. They propose a change for the
owner instead. Contributor drafts remain revisable; governed mode keeps the
existing contributor revision policy. Retries check the current mode and the
validated latest revision before writing.

The service stores immutable revision comments, server-generated owner decision
evidence and a hash-chained governance sidecar. Backup/restore includes the
governance history and operation receipts. A restore under a new project name
retains historical hashes and validated source ranges rather than rewriting
decisions. A retry after an interrupted write checks the exact original receipt
and uses a separately guarded recovery operation; uncertainty without a matching
receipt remains unresolved. Recovery is bound to the human account, so the same
owner can log in again and retry the same key. Current session authority and owner
membership are checked under the authority lock before any replay or effect.

If a creation fails before any native row is allocated, the owner who attempted
it can use **Clear failed creation**, give a reason, and start a new creation.
The original attempt and its release audit are retained. Failed creations show
their recorded title and time when available. An interruption before the receipt
write has its own **No creation receipt was written** message. An allocated row,
even an empty one, cannot be cleared here: reload the requirements and, if the
creation is still incomplete, ask the host operator to reconcile it.

Clearing has no age gate. The receipt is written before the native create, and
a live endpoint holds the project lock, so clearing a never-written receipt
does not widen that race. A native create outliving the service's time limit
while an already-written pending receipt is cleared was not reproduced with
the pinned tracker. This does not prove that every timed-out process has ended.
If a requirement row appears after a clear, the host operator should inspect
the original operation receipt, retained release audit, native row request
label and revision comments, and the endpoint/native process outcome before
reconciling it. Do not clear an allocated row or infer its absence from a failed
native read.

### Withdraw or supersede an accepted requirement

In simple mode, the signed-in project owner can withdraw a currently accepted
requirement with a reason, or supersede it with another currently active accepted
requirement in the same project. Pending drafts and narrative sections cannot be
terminal targets. The replacement is pinned by its ID, revision and content hash.
It is never substituted into existing references automatically.

The page requires confirmation before either action: **This cannot be undone in
the browser. If this is a mistake, create a new requirement. The old wording and
history remain available.** There is no reactivation route. Governed mode has no
withdrawal or supersession command in this release; changing mode never removes
a recorded terminal state. The governed operator acceptance and publication
commands retain their existing authority and cannot reactivate a terminal record.

Acceptance and status are separate. Terminal records retain their immutable
accepted revisions and decisions. A member can read the reason, author, timestamp,
exact replacement and revision history. Reverse “Supersedes” links are derived
from the old record's pointer. A failed or conflicting transition reads as
**Status unknown**, and stays out of current nonterminal selection. A pending
transition belongs to the project. Any current human owner in simple mode can
finish the exact saved transition, including after a page reload or the original
owner's departure. No remembered key or new reason/successor is used. Abandon
only after the saved server deadline (service time plus the endpoint and native
timeouts), when a complete native read under the same locks proves no state was
written. The page gives the retry time. The service stops the endpoint process group,
including native children, on timeout or return. The receipt keeps its released disposition.
Once a state was written, finish is the only action. A written supersession retains
its exact replacement even if that successor later changes; its missing reason or
label can still be finished. Before the state is written, the successor must still
be eligible. A pending predecessor reserves its successor against editing,
withdrawal and supersession until the predecessor is finished or abandoned.
If a prior writer's state appears after release, that native decision overrides
release: the transition is pending again and remains finishable, whether or not
the requirement was edited, accepted or withdrawn afresh in between. Any current
human owner in simple mode, and the host operator's `admin.py requirement-reconcile`,
can finish the exact saved decision, and an operator release is refused rather
than reported as success once the state is known to have landed. Finishing writes
the missing reason and label if they never landed; the recovery route is itself
the native writer that outlives both the process-group stop and the deadline. When
a later decision on the same requirement has completed, a late state of an earlier
abandoned decision is superseded, not finishable: the requirement reads the later
decision's status, the page offers no finish, and the owner's finish and the
operator's complete are refused with a sentence naming the later decision. Only
without a completed later decision does a late state beside a second conflicting
state stay unknown and point at the same reconciliation, never at a requirement
left with no pending transition and no command. Finishing a late state that would
also withdraw a revision accepted after it names that revision and when it was
accepted, and an owner must confirm it explicitly. Damaged receipts
are identified separately and affect their own requirement only; unidentifiable
files are operator diagnostics, never evidence that every requirement is pending.
`admin.py requirement-reconcile` finds the terminal receipt by its operation ID.
For this recovery, the host operator supplies `--state` with the existing web
service state document. The command takes its authority lock and the project lock,
then either finishes the saved evidence (`--disposition complete`) or retains an
abandonment disposition (`--disposition released`) after the same deadline and
native absence proof. For terminal recovery, `failed` is an alias for `released`;
neither permits reuse of the abandoned transition's original key. Use a new intent.
Completion retains the finisher, time and operator `--reason`, separately from
the immutable original decision author and reason. The service audit includes
the requirement, original operation and recovery operation. Operator audit entries
are derived from verified durable receipt history, so a running service cannot
overwrite them with an older in-memory audit tail. Receipt integrity covers
status, release and completion history; restore refuses altered metadata.
Before withdrawal or supersession of a pending edit, accept the pending edit first.

| Consumer | Terminal requirement behavior |
| --- | --- |
| Requirements list, detail and generated BRD | Retain accepted content and history; display status separately. `nonterminal_ids` excludes terminal and unknown records but includes never-accepted drafts, pending edits and narrative sections; it is not an accepted-requirements selector. The BRD groups terminal records separately. |
| Work and brief | Native requirement tasks stay visible with their status labels. A terminal transition neither closes work nor changes checkpoints, blockers or lifecycle facts. |
| Proposal list, detail, brief and traceability | Exact old content remains readable with terminal or unknown state and a sentence. These states are never presented as draft. New incorporation into any of them is refused; unrelated proposals remain readable. |
| Capability links | An explicit historical revision remains linkable. A link without a revision requires a currently active record. |
| Offline export and publication | Explicit historical selections retain their manifest bytes and acceptance even while current status is unknown. Provenance includes the validated state/reason pair and governance lineage when provable, otherwise an unknown-state sentence; terminal status does not delete historical content. |
| Impact analysis and worker launch contracts | Operate on explicitly selected immutable manifests and exact references. Terminal status does not rewrite a manifest, silently retarget work, or remove a launch requirement. A changed active selection needs a new reviewed manifest and reassessment. |

Backup/restore carries native evidence, the exact request receipts and governance
source ranges. It preserves terminal hashes when the project is restored under a
new name. Projects with no attempted terminal transitions retain preceding-release compatibility.
From the first **attempted** withdrawal or supersession, **do not serve or restore
the project with an older kit**, including an attempt interrupted before any native
state was written. The preceding reader refuses terminal evidence, but its writer does
not: editing and accepting can succeed and permanently damage terminal history.
The preceding kit can also restore a backup with terminal records without warning,
then read those records as unreadable. Use this release or a later compatible one
for those backups. Do not strip evidence to make it readable. Restore a pre-transition
backup taken before the first attempted transition if a rollback is required,
retaining the later backup separately. Named
baselines and Markdown download/comparison are a separate later delivery.

An interrupted acceptance uses its recorded generated decision ID. Editable
native titles, descriptions, labels and creator metadata cannot substitute a
different decision. A missing or unreadable recorded decision, or an older
pending attempt without a durable binding, stays unknown and needs host operator
reconciliation. Retrying does not generate another decision.

Governance reads warn when an entry is more than the existing 24-hour clock skew
bound ahead of the host clock. Ask the host operator to check the clock and
history. Reads and writes continue; revision numbers and hashes order authority,
and the service does not rewrite the stored history.

The generated document shows the last accepted text first. A newer draft is a
separate pending edit, with its author kind and native revision time beside
**Accept** when available. The owner's account uses its display name, or
**your own edit** for that owner. A contributor's supplied name is explicitly
**not verified**, including names resembling an account ID. Missing or ambiguous
attribution remains unknown; an unavailable time is stated. Changing modes
preserves the revision. Its Decisions section
uses immutable acceptance evidence for the accepted revision, including when a
later draft exists. An unreadable requirement is marked by ID;
healthy requirements remain readable and an affected acceptance is never trusted.
On an empty project, **Start requirements** creates a parent task through the
ordinary authorized task route, then the owner can add content on this page.

The authority store and project files are trusted server storage. A shell that
can modify them can bypass these checks; forced contributor keys cannot. Human
session cookies need CSRF protection on writes. A human session explicitly sent
as an Authorization bearer does not need CSRF: browsers do not attach that header
automatically. This does not give service credentials or agent tokens owner rights.
The host's operator requirement-apply/backfill routes remain available in both
modes and keep their operator allowlist and exact evidence requirements.

This slice supplies direct editing, acceptance, withdrawal and exact supersession.
Baseline controls, downloadable baseline comparisons and proposal/room acceptance
controls await their separate releases.

For the existing host export command, selections containing owner acceptance
also supply `--governance SNAPSHOT.json`: a captured files map containing
`.requirements-governance.json` and, after a restore under another name,
`.requirements-governance-source.json`. The exporter validates the same history
and project binding as the live reader and retains those files in its provenance.
Historical acceptance does not depend on today's membership or mode.

Read the current records or capture that files map with the same kit's client:

```sh
python client.py --config client.local.json --project example --actor reader -- requirements list
python client.py --config client.local.json --project example --actor reader -- requirements get REQUIREMENT_ID
python client.py --config client.local.json --project example --actor reader -- requirements brd
python client.py --config client.local.json --project example --actor reader -- requirements governance
python client.py --config client.local.json --project example --actor reader -- requirements snapshot > SNAPSHOT.json
```

These commands only read. Existing projects and restores from backups without a
governance sidecar remain governed. A creation started before this release also
remains governed when resumed; only a durable new creation intent opts into simple
mode. Restore preserves source governance and acceptance hashes and records the
destination lineage for decisions made after restoring under a new name.

## Validate and publish a draft

From the kit directory, using Python 3.10 or newer:

```sh
python requirements.py docs/requirements-baseline.json
python publish_brd.py docs/requirements-baseline.json --output dist/requirements
```

The publisher writes a named directory containing the exact manifest content, readable BRD and a receipt with hashes of the emitted bytes. `current.json` points to the last successfully published directory and its receipt hash. Repeating the same publication is safe: existing files must match exactly. Reusing a name with different content fails; select a new baseline name for a revision.

The generated draft uses a deterministic layout. It does not reproduce the manual bootstrap BRD's formatting. Neither publication nor a passing validator changes any requirement's acceptance state.

## Accepting content

First reconcile proposals in Beads and prepare a candidate snapshot with the intended accepted revisions and baseline state. Validate its record and manifest hashes using the contract. Present that exact manifest hash to the project owner(s). Record their decision and evidence, then supply a separate acceptance JSON object:

```json
{
  "manifest_sha256": "<exact candidate manifest hash>",
  "decision_id": "<canonical owner decision ID>",
  "owners": ["project-owner"],
  "approvers": ["project-owner"],
  "policy": "any-owner",
  "evidence": "<durable pointer to the owner's acceptance of that hash>"
}
```

This is a template, not approval evidence. Every selected record must be accepted; approvers must be named owners. `all-owners` requires every listed owner. The same owner may contribute and approve. The tool checks the structure and hash binding of these declarations; existing account and repository controls remain responsible for authorization.

```sh
python publish_brd.py candidate.json --acceptance acceptance.json --output dist/requirements
```

Record the resulting receipt and manifest hashes back in Beads before describing the publication as registered there. The offline pointer alone is local publication evidence. Server acknowledgement/recovery automation is later work.

## Recovery and changes

An interruption before pointer replacement leaves the previous current publication intact. Retry the same inputs to finish a complete candidate that was written before interruption. Temporary staging directories are never current. Keep historical named publications; do not edit their files in place.

When code exposes a problem, record a linked defect, ambiguity or scope-change proposal in Beads, with the affected requirement IDs/revisions and original assertion. An accepted change gets a new source revision and baseline; earlier snapshots remain reproducible. `requirement_impact.py` traverses an explicit work graph and emits reassessment results. Automatic writes of those results into task state and lifecycle tables remain separate pending features. Unrecorded implementation/test/review/integration/deployment/live-verification facts remain unknown.
