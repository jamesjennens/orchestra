# Working with requirements snapshots

The validator and publisher operate over explicit structured snapshots. Beads remains canonical for requirements, discussion and decisions. The [integration workflow](REQUIREMENTS_INTEGRATION.md) now resolves versioned revision comments from a native export, computes impact/reassessment views and gates worker launches on canonical plan acknowledgement. Arbitrary prose is never guessed into revision fields; server publication acknowledgement remains explicit.

The [BRD](BRD.md) and [source snapshot](requirements-baseline.json) are draft-0.1. They are not an accepted specification. The [contract](REQUIREMENTS_CONTRACT.md) records the implemented boundary and the disposition of the independent review.

## Editing a project's requirements in the web interface

In simple mode, a signed-in project owner opens **Requirements**, edits the text
and saves a draft. **Accept** records the owner's decision for that content.
Editing accepted text creates a new draft; the earlier accepted content remains
available to existing exact references. The business requirements document is a
read-only view of the records, including narrative, requirements, open questions
and decisions. Every project member, including viewers, can read it.

Newly provisioned projects start in simple mode. Existing projects with no
governance history remain governed. An owner can switch modes from the page;
changing modes preserves history and never accepts a draft automatically.
Credentials, agents and superusers without owner membership cannot use these
editing controls. The host's governed publication commands below retain their
existing operator authority.

The service stores immutable revision comments, server-generated owner decision
evidence and a hash-chained governance sidecar. Backup/restore includes the
governance history and operation receipts. A restore under a new project name
retains historical hashes and validated source ranges rather than rewriting
decisions. A retry after an interrupted write checks the exact original receipt
and uses a separately guarded recovery operation; uncertainty without a matching
receipt remains unresolved. Private review evidence contains verification detail.

This slice supplies direct editing and acceptance. Withdrawal, supersession,
baseline controls, downloadable baseline comparisons and proposal/room acceptance
controls await their separate releases.

For the existing host export command, selections containing owner acceptance
also supply `--governance SNAPSHOT.json`: a captured files map containing
`.requirements-governance.json` and, after a restore under another name,
`.requirements-governance-source.json`. The exporter validates the same history
and project binding as the live reader and retains those files in its provenance.
Historical acceptance does not depend on today's membership or mode.

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
