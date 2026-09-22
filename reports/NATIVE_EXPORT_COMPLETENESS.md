# Native export-completeness investigation (kittrial-5bb.4)

Date: 2026-09-22. Environment: koopa, kit `/home/james/beads-team-pilot/kit`
VERSION 0.1.0, runtime `/home/james/beads-team-pilot/runtime`, real installed
endpoint, disposable MySQL-backed scratch project (`disptest`, removed after
the run). Production `kittrial` was never written to. Driver scripts:
`native_export_investigation.py` (full sweep) and `native_failrefresh.py`
(failing-refresh check), both runnable on the koopa host against any
disposable project name.

## Append-only correction of earlier claims

The first contribution (756187a) and its checkpoint claimed "concurrent
refresh consistency" and "export roundtrip" based on renderer-level tests
only: sequential in-process renders and a local file copy. That evidence does
NOT exercise native `bd export`, the shared endpoint, its locks, or client
transport, and did not support those claims. Corrected scope: the earlier
tests prove renderer-level identity-set agreement only; native completeness
is established by the investigation below, not by those tests.

## Native evidence (one controlled snapshot, scratch project)

- Controlled snapshot: 5 tasks x 20 comments (1,024-50,024 char bodies),
  later runs accumulated to 26 tasks / 403 comments via re-init; all checks
  ran against the accumulated snapshot too.
- Native read: `bd export --all` raw bytes 4,140,662, sha256
  `8b8f7b0a13e2f8c0d611e6dfeb965bd461baa44da0ac00c5262a72e642f02617`,
  26 issues / 403 comments (identity sets captured).
- Endpoint `refresh` returned `{'issues': 26, 'comments': 403}` matching the
  native counts; `view issues.jsonl` returned 4,145,329 bytes whose parsed
  identity sets EQUAL the native export's issue/comment identity sets.
- Byte-level note: the view file's bytes differ from the export stdout
  (sha256 `5ea655b0…` vs `8b8f7b0a…`); completeness is identity-set and count
  equality, not byte equality — the writer re-serializes rows.
- Large-body roundtrip: the largest comment (50,024 chars) roundtrips intact
  through export -> refresh -> view with no truncation.

## Concurrency at the shared endpoint

3 parallel endpoint `refresh` calls plus 1 parallel `view` read: all four
returned rc=0 (endpoint serializes refreshes on `.refresh.lock`; views are
written atomically via `os.replace`). After concurrency, the view's identity
sets still equal the native export's.

## Failing refresh

With views present, `bin/bd` was made unavailable so the refresh's native
export failed: the endpoint returned an explicit failure (rc=2,
FileNotFoundError surfaced in stderr) and the prior `issues.jsonl` was
unchanged (sha256 identical before/after). A failed refresh preserves prior
views rather than corrupting or clearing them.

## Conclusion

Not reproduced: no export-completeness defect (missing comments, counts
below native, truncation) was found at this kit version (0.1.0, endpoint at
base 20aefa0) across a 4.1 MB / 403-comment snapshot, concurrent refreshes,
and a failing refresh. Boundary: single-host MySQL-backed deployment, one
scratch project, one snapshot per run; multi-project and production-scale
behaviour remain untested here. If a real "refresh total exceeded export"
report recurs, capture both raw artifacts (refresh summary and export file)
at the time of the mismatch; this investigation shows the toolchain reports
matching counts when both sides come from one snapshot.
