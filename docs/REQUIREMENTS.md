# Working with requirements snapshots

The first slice is an offline validator and publisher over explicit structured snapshots. Beads remains canonical for requirements, discussion and decisions. The tools do not yet turn arbitrary Beads exports into revision manifests or update server publication state.

The [BRD](BRD.md) and [source snapshot](requirements-baseline.json) are draft-0.1. They are not an accepted specification. The [contract](REQUIREMENTS_CONTRACT.md) records the implemented boundary and the disposition of the independent review.

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

When code exposes a problem, record a linked defect, ambiguity or scope-change proposal in Beads, with the affected requirement IDs/revisions and original assertion. An accepted change gets a new source revision and baseline; earlier snapshots remain reproducible. Automatic impact traversal and lifecycle tables are separate pending features. Unrecorded implementation/test/review/integration/deployment/live-verification facts remain unknown.
