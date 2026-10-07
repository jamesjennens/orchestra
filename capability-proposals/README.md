# Capability proposal payloads carried by a delivery

A contribution that adds or changes something a user or an agent can rely on carries its
capability proposal payload **as a file in the delivery**, not as a live `capability propose`
(kittrial-5bb.179; see §13.1 of [the capability design](../docs/CAPABILITY_INDEX_DESIGN.md)).
The convention this directory uses: **one JSON file per record, named `<key>.json`**, and the
contribution summary names them.

Why a file and not a live write: with no `--key`, the release check set covers every accepted
and draft record, so a record written from a lane is checked against main while its work is
still in review. Its pointers are missing, and it reads `drifted` at every release cut before
that work is integrated. Carrying the payload as a file has no such window: the reviewer reads
it with the diff, and the coordinator writes it into the index — `capability propose` for a new
record, `capability revise` for an existing one — **after integration**, then accepts the
meaning at release with the release as evidence.

## What a payload file is

The exact `propose`/`revise` payload, and nothing else:

- the closed record set: `key`, `name`, `aliases`, `summary`, `requirements`, `anchors`,
  `code`, `tests`, `owner`, `tags`; plus
- the writer's own fields: `schema_version`, `operation_id`, and for a revision `revision`
  and `expected_sha256` (the content hash of the revision it replaces);
- **not** `operation` (the command supplies it) and no other field: there is no field for
  "the check that proves it" and none for the commit. `capability check` resolves the
  pointers at the commit it is run against and records that commit itself.

`code`, `tests` and `anchors` must exist at the delivered commit, never on a branch still
under review, and `tests` must include a test that fails when the capability breaks. A
regression fails that test; the check still passes, because the check proves **location, not
meaning** — the reviewer judges the claim with the change.

The coordinator writes one after integration, from the kit:

```sh
python client.py --config client.json --project PROJECT --actor ACTOR -- \
    capability revise --file capability-proposals/office.install-guards.json
```

## Single-use, bound to this project's revision, deleted after acceptance

A file here is a **carrier, not the record**, and it is spent the first time it is used:

- **Single-use and bound.** A file that revises an existing record carries `revision` and
  `expected_sha256`, the content hash of the revision it replaces. That compare-and-swap is
  spent once the coordinator writes it: a second attempt is refused as stale, because the
  index has already moved on. The `expected_sha256` names **this project's** index revision,
  so the same file is refused on any other project, and on this one once that revision
  changes again. A file meant as a new record (`capability propose`, revision 1) is
  likewise written once.
- **The index is the source.** After a release accepts an entry, the coordinator **deletes
  that file in the commit that follows the release**. The accepted record lives in the
  project's index, and reads come from there; the file is only the copy a reviewer and a
  coordinator needed while the payload was in flight. A file left behind after acceptance is
  stale by construction, so the folder is emptied as entries are accepted rather than kept
  as a second, drifting list.
- **Before acceptance** the file is the reviewer's and the coordinator's copy of the pending
  payload. Nothing was accepted or verified by placing it here.

## The payloads in this delivery

Twelve draft records are carried for the 2026-10-05/06 releases. None was accepted or
verified. Each file here is the corrected revision-2 payload with the four wording
corrections of revision 3 applied, ready for the coordinator to write after integration and
accept at release:

| File | Record | What this revision corrects |
| --- | --- | --- |
| `office.release-bd-glibc-target.json` | kittrial-3k5 | revision 3: says "For a target with an older glibc the office release bundles" a statically linked bd, the archive `versions.json` pins as `bd_static` — not every release bundling it |
| `office.service-network-address.json` | kittrial-8m7 | no content change; owner set |
| `office.connection-deadline-and-bound.json` | kittrial-2gt | a silent connection is closed after 30 s, not merely "cannot hold the interface down"; adds the kittrial-5bb.175 sentence and the reaper pointer (a starting thread is never taken for a dead one) |
| `http.answers-under-held-lock.json` | kittrial-s7z | no content change; owner set |
| `deployment.damaged-file-one-fault.json` | kittrial-a2y | revision 3: only a regular file is read at the configuration path, and a FIFO or directory in its place is refused at once; points at the reader in `admin.py` (`deployment_document`, `ConfigurationUnreadable`) and the endpoint's one fault mark, not at `project_creation.py` sentences |
| `release.group-size-from-writes.json` | kittrial-4w5 | no content change; owner set |
| `review.one-recommendation-per-person.json` | kittrial-2pc | no content change; owner set |
| `review.follow-on-base-refusal-whole.json` | kittrial-6lh | revision 3: the later integration is one "recorded by a listed operator and not reverted (kittrial-5bb.155)", not any integration the project recorded later |
| `docs.own-document-bound.json` | kittrial-oei | no content change; owner set |
| `recovery.restore-new-unusable-sidecar.json` | kittrial-bfr | no content change; owner set |
| `open.items-names-reserved.json` | kittrial-dox | no content change; owner set |
| `office.install-guards.json` | kittrial-0oz | revision 3: "at the end of prepare" dropped, as it holds only for an existing deployment; the runtime's `bin` and the caller's `PATH`, not the service's PATH; "before it writes anything" scoped away from prepare; adds `admin.py::require_bd_init_tools` and `http_service.py::runtime_service_lock` |

## Owner

Every payload names `person:orchestra-coordinator`, the durable owner identity already carried
by the coordinator's other capability records. A bare session actor cannot be an owner: the
record validator refuses one (`owner must be a durable identity, account:<uid> or
person:<name>`), and it refuses `person:session-...` too (`not a session actor`). The
coordinator's accountable person for these records is that durable identity.
