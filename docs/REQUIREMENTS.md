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
The original attempt and its release audit are retained. An allocated row, even
an empty one, cannot be cleared here: retry the original creation and, if that
still fails, ask the host operator to reconcile it.

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
**Status unknown**, and stays out of current active selection. Retry its exact
original request; an unproved receipt or effect needs host operator reconciliation.

| Consumer | Terminal requirement behavior |
| --- | --- |
| Requirements list, detail and generated BRD | Retain accepted content and history; display status separately. Current `active_ids` excludes terminal and unknown records. The BRD groups them separately. |
| Work and brief | Native requirement tasks stay visible with their status labels. A terminal transition neither closes work nor changes checkpoints, blockers or lifecycle facts. |
| Proposal traceability | Exact old content remains readable; it no longer qualifies as the content accepted today for incorporation. |
| Capability links | An explicit historical revision remains linkable. A link without a revision requires a currently active record. |
| Offline export and publication | Explicit historical selections retain their manifest bytes and acceptance. Provenance includes the validated state/reason pair and governance lineage; terminal status does not delete historical content. |
| Impact analysis and worker launch contracts | Operate on explicitly selected immutable manifests and exact references. Terminal status does not rewrite a manifest, silently retarget work, or remove a launch requirement. A changed active selection needs a new reviewed manifest and reassessment. |

Backup/restore carries native evidence, the exact request receipts and governance
source ranges. It preserves terminal hashes when the project is restored under a
new name. Projects with no terminal records remain compatible with the preceding
release. A project with terminal evidence requires this release or a later one:
the preceding reader refuses the newly activated state family. Do not roll such
a project back and infer activity from old labels, or strip evidence to make it
readable. Restore a pre-transition backup if a rollback is required. Named
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
separate pending edit. Its Decisions section uses immutable acceptance evidence,
not editable native decision titles. An unreadable requirement is marked by ID;
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

This slice supplies direct editing and acceptance. Withdrawal, supersession,
baseline controls, downloadable baseline comparisons and proposal/room acceptance
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
