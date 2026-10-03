# Native export-completeness investigation (kittrial-5bb.4)

Date of execution: 2026-09-22T03:19–03:23 UTC on the coordination server.
Method: ephemeral in-process probe + separate failure-path script, both run
from a scratch checkout and removed with the scratch project; NO drivers
are committed in this candidate (a prior revision's committed drivers were
unsafe and have no place in this commit). Scratch MySQL-backed project was
removed at the end of the run; the shared production installation was not
written to, and was verified intact afterwards (single `bd` binary
present, no leftover renamed copy).

## Provenance and boundaries (stated exactly)

- Kit version file: 0.1.0; `bd` 1.2.2; Dolt 2.2.0.
- Probe imports (`endpoint`, `admin`): `endpoint.py` and `render.py` as
  committed at 756187a, byte-identical to the base 20aefa0 blobs of the
  same files (same Git blob ids at both commits). LF Git-blob sha256
  (`git show REV:FILE | sha256sum`, no line-ending conversion):
  `endpoint.py` (8,298 bytes) sha256 `d56af223f4b18b4f7951ebee6a7f721a370d89ba5405494ced77055832a73b38`,
  `render.py` (6,612 bytes) sha256 `5f0b3ce12aef42646c5382950670fbce1b7eb40a2d88bef1682c9c056ee3a769`.
- Invocation: in-process `endpoint.execute()` calls, NOT through the
  SSH/client transport. Installed-endpoint and transport equivalence are
  qualified, not proven.
- Scratch database: disposable MySQL database served by the Dolt
  sql-server for the probe's own project; dropped with the project.

## Native evidence (one controlled snapshot)

- Controlled input: 5 tasks x 20 comments per seeding round (bodies 200 or
  50,024 chars); seeding rounds repeated against the same scratch project,
  accumulating to 26 tasks / 403 comments — the totals below are the
  accumulated snapshot, not 5 x 20 (each prior round's rows persisted in
  the scratch database because re-init preserved it).
- Native read `bd export --all`: 4,140,662 bytes, sha256
  `8b8f7b0a13e2f8c0d611e6dfeb965bd461baa44da0ac00c5262a72e642f02617`,
  26 issues / 403 comments (identity sets captured).
- Endpoint `refresh` returned `{'issues': 26, 'comments': 403}` — equal
  to the native counts.
- Endpoint `view issues.jsonl`: 4,145,329 bytes; parsed identity sets
  EQUAL the native export's issue/comment identity sets.
- Byte-level note: view bytes differ from export stdout (sha256
  `5ea655b0…` vs `8b8f7b0a…`). Completeness is identity-set and count
  equality, not byte equality — the view writer re-serializes rows.
- Content roundtrip: the largest comment body (50,024 chars) roundtrips
  intact through export -> refresh -> view; the final runs compared the
  FULL text, not length only (earlier runs asserted length only).

## Concurrency and failure behaviour

3 parallel endpoint `refresh` calls plus 1 parallel `view` read: all four
completed with rc=0 (the endpoint serializes refreshes on `.refresh.lock`;
views are written atomically via `os.replace`). After concurrency, the
view's identity sets still equal the native export's. Failure-path check:
with views present, a missing `bd` executable surfaced as an explicit
rc=2 `FileNotFoundError` and prior views were byte-identical (hash
unchanged) — a failed refresh preserves prior views rather than
corrupting or clearing them.

## Conclusion (not-reproduced, qualified)

No export-completeness defect (missing comments, counts below native,
truncation) was found across a 4.1 MB / 403-comment native snapshot,
concurrent refreshes, and a failing refresh, with in-process endpoint
calls at base 20aefa0 code. Boundary: single-host MySQL deployment, one
scratch project, one snapshot per run, in-process invocation (no SSH
transport), installed-kit endpoint not independently exercised. If a real
"refresh total exceeded export" report recurs, capture both raw artifacts
(refresh summary stdout and export file) at the time of the mismatch;
this investigation shows the toolchain reports matching counts when both
sides come from one snapshot.
