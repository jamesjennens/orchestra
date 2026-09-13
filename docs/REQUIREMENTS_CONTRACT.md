# Offline requirements contract, version 1

This is an exploratory implementation contract derived from draft-0.1, not owner acceptance of that baseline. The original BRD and source manifest remain unchanged. Beads remains the source of requirements; this slice validates captured snapshots, without introducing a new server schema or writing requirement revisions.

## Review disposition

Hermes review on kittrial-pth.14, comment `01a0995f-79bc-7a0e-b709-f6467636c5c0`, is complete. Its findings are retained as proposals, with these dispositions:

- F1: clarify the separation between content and acceptance. The content manifest cannot include a later decision accepting its own hash. A separate acceptance object cites the manifest hash and deciding record. A publication receipt carries artifact hashes. Drafts need no invented acceptance decision or empty change proposal.
- F2: document type labels separately from state labels. `requirement` and `brd-section` already distinguish records; `requirement:draft` alone does not erase that distinction. Future feedback records use `change-proposal`, with classification defect, ambiguity or scope-change. Absence of feedback before a discovery is not a defect. Server label migration is outside this slice.
- F3: require named owners and recorded evidence for accepted publication, but do not require author and approver to differ. A project owner may contribute and approve. SSH/repository controls provide access boundaries; actor strings and offline evidence are declarations, not authenticated identities. Tools check structural evidence, not the truth of an approval.
- F4: the implementation table is a roadmap, not a claim of delivery. Lifecycle integration remains later scope; absent lifecycle facts are unknown. Revision reassessment must not infer implemented/tested/reviewed/integrated/deployed/live-verified from issue status.
- F5: verify exact artifact bytes and put their hashes in a separate receipt. Embedding the whole file's own hash inside it is circular. UTF-8 with LF is the publisher output convention; the manually generated old BRD is not rewritten.

## Validator API

`requirements.py` exports `ValidationError(ValueError)`, `canonical_bytes(value)`, `content_hash(mapping)` and `validate_manifest(manifest, acceptance=None)`. Validation returns None on success and raises ValidationError on malformed inputs. A CLI accepts a manifest JSON path and optional `--acceptance` JSON path; invalid input exits nonzero without a traceback.

Canonical bytes are UTF-8 JSON with sorted keys, ensure_ascii=False, separators comma/colon and allow_nan=False. `content_hash` excludes only the mapping's top-level `sha256`. Nested record hashes remain part of manifest identity. Every recorded hash must be lowercase 64-character SHA-256 and must match.

Support the existing schema_version 1 manifest without alteration. Required fields: schema_version (integer 1), baseline (nonempty string), state (draft or accepted), canonical_project, job, authority, hash_convention (nonempty strings), narrative (list), requirements (nonempty list), sha256. Both lists contain objects with id, title, description (nonempty strings), revision (positive integer, never boolean), acceptance_state (draft or accepted), sha256. Requirements also have unique nonempty key. IDs are unique across both lists. Baseline names are safe directory components: `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`, excluding `.` and `..`. Unknown fields are rejected, except optional `references` on records.

Optional record `references` is a list of exact `{id, revision, sha256}` objects resolving to a selected record in the same snapshot. No implicit latest revision, unknown target, revision/hash mismatch or duplicate reference. References are structural; cycles are not inherently invalid for requirements (impact graph semantics belong to later work).

For state accepted, every selected record must have acceptance_state accepted and a separate acceptance object is mandatory. For state draft, no acceptance object is permitted, avoiding a misleading approved draft. The acceptance object's exact fields are: manifest_sha256, decision_id, owners (nonempty list of unique nonempty strings), approvers (nonempty list of unique nonempty strings, subset of owners), policy (`any-owner` or `all-owners`), evidence (nonempty string). manifest_sha256 must match this manifest; all-owners requires equal owner/approver sets. decision_id and evidence are durable pointers supplied by the operator. This is trusted-team policy validation, not proof of identity. Rejection/blocking records are not a supported input in version 1; callers must resolve them before selecting accepted content. Passing a draft through this validator does not accept it.

Native Beads exports carry description, labels and comments; they do not supply this structured revision contract automatically. The [native integration extension](REQUIREMENTS_INTEGRATION.md) resolves explicit versioned revision comments with exact selections and retained provenance. It does not derive revision/acceptance fields by guessing prose. Historical baseline catalogs keep old evidence resolvable through successive impact analyses.

## Publisher boundary

`publish_brd.py` consumes one validated manifest and optional acceptance file, entirely offline. It renders exact narrative and requirement descriptions with source IDs, revisions, hashes and conspicuous baseline state. It retains the input content identity, but need not reproduce the manually laid-out bootstrap Markdown byte for byte.

CLI: `python publish_brd.py MANIFEST --output DIRECTORY [--acceptance FILE]`. Python API: `publish(manifest, output, acceptance=None)` returns the publication directory as a Path. Failures raise ValueError or OSError; the CLI reports them without a traceback. Reject duplicate JSON keys and invalid Unicode at file boundaries rather than silently changing the selected content. Reject symlink publication paths and platform-reserved baseline names (including Windows device names and trailing dots), even on Linux, so publications are portable.

Each named publication directory contains `manifest.json`, `BRD.md`, optional `acceptance.json`, and `receipt.json`. JSON and Markdown are UTF-8/LF. The receipt contains schema_version, baseline, manifest_sha256, state, and a files mapping of file names to SHA-256 of exact bytes. It does not include its own hash. The root `current.json` points to the baseline plus receipt hash.

Markdown normalizes source CRLF/CR line endings to LF; the manifest retains the exact original strings and content identity. `current.json` is a reserved baseline name. Verify staged bytes before installation and propagate file-sync errors. Recovery checks cover process interruption on a local filesystem, not power-loss durability, hostile filesystem races or network-filesystem semantics.

Stage a complete directory beside the destination, then rename it into place. Only then atomically replace current.json. Never overwrite an existing named publication; identical retries verify all bytes then repair/advance the pointer. A conflicting retry fails without changing current. Interrupted staging is never current; a completed directory with no pointer is recoverable by retry. Independent writers racing for the same name must either agree exactly or fail. Current is the last successful explicit publication, with no chronological ordering guarantee. Database acknowledgement is outside this offline tool: current.json means locally published, not recorded or accepted on the server.

Tests must cover real baseline acceptance, malformed input, tampering, revision/reference mismatches, trusted-owner policy, byte reproducibility, traversal rejection, conflicting retries and interruption before pointer replacement. No server restarts or migrations.
