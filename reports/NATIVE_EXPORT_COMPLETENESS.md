# Native export-completeness investigation (kittrial-5bb.4)

Date of execution: 2026-09-22T03:19–03:23 UTC. Investigation host: the
coordination server (`koopa`). Scratch MySQL-backed project under a
temporary directory, removed at the end of the run; the shared production
installation was not written to. No investigation drivers are committed:
an earlier revision shipped two operational scripts that renamed the
shared runtime's `bd` binary and dropped a database at import time; they
were removed in this revision (see correction below) and any future rerun
must use a fully separate disposable runtime, never the shared one.

## Append-only correction of earlier claims

The first contribution (756187a) claimed "concurrent refresh consistency"
and "export roundtrip" based on renderer-level tests only: sequential
in-process renders and a local file copy. That evidence does not exercise
native `bd export`, the shared endpoint, its locks, or client transport,
and did not support those claims. This revision replaces those claims with
the native evidence below and states the boundary of the renderer tests:
they prove renderer-level identity-set agreement only.

Second correction (this revision): the previous revision of this report
committed two operational drivers that were unsafe against a shared
installation (they renamed the shared `bin/bd` and dropped a fixed
database name at import). They have been removed from the candidate. The
investigation results below were produced before their removal; the
shared installation was verified intact afterwards (single `bd` binary
present, no leftover renamed copy).

## Provenance and boundaries (stated exactly)

- Kit under test: VERSION 0.1.0; `bd` 1.2.2; Dolt 2.2.0.
- The probe imported `endpoint`/`admin` from a branch checkout at
  756187a, NOT from the installed kit directory. At that revision the
  branch's `endpoint.py` and `render.py` are byte-identical to base
  20aefa0 (`04e31967…`, `71edbdf9…`); the installed kit's copies have
  since diverged through the separate integration of kittrial-5bb.2
  (`75c087c0…`, `5f0b3ce1…`), which did not touch refresh/view/export
  code paths. Installed-endpoint equivalence is therefore qualified,
  not proven.
- The probe called `endpoint.execute()` in-process. It did not exercise
  the SSH/client transport boundary.
- Scratch database: unique-name disposable MySQL database served by a
  Dolt sql-server started for the probe's own runtime directory.

## Native evidence (one controlled snapshot)

- Controlled snapshot: 5 tasks x 20 comments per run (bodies 200 or
  50,024 chars); repeated runs accumulated to 26 tasks / 403 comments;
  all checks passed against the accumulated snapshot.
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
  intact through export -> refresh -> view; full-text equality was
  verified on the final runs (earlier runs asserted length only).

## Concurrency and failure behaviour at the shared endpoint

3 parallel endpoint `refresh` calls plus 1 parallel `view` read: all four
completed with rc=0 (the endpoint serializes refreshes on `.refresh.lock`;
views are written atomically via `os.replace`). After concurrency, the
view's identity sets still equal the native export's. Failure-path run:
with views present, a missing `bd` executable surfaced as an explicit
rc=2 error and prior views were byte-identical (hash unchanged) — a
failed refresh preserves prior views rather than corrupting or clearing
them. That run's driver has been removed as unsafe to rerun (see
correction above).

## Conclusion

Not reproduced: no export-completeness defect (missing comments, counts
below native, truncation) was found across a 4.1 MB / 403-comment native
snapshot, concurrent refreshes, and a failing refresh, at kit 0.1.0 with
in-process endpoint calls at base 20aefa0 code. Boundary: single-host
MySQL deployment, one scratch project, one snapshot per run, in-process
invocation (no SSH transport), installed-kit endpoint not independently
exercised. If a real "refresh total exceeded export" report recurs,
capture both raw artifacts (refresh summary and export file) at the time
of the mismatch; this investigation shows the toolchain reports matching
counts when both sides come from one snapshot.
