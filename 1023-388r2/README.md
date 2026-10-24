# Review kittrial-5bb.388 r2 (rooms slice 2: items and acceptance) - delivery e274992 on main 3a0c5b3, trial 877896d

Verdict: CHANGES REQUESTED. Reviewer: coordinator-spawned adversarial reviewer, 2026-10-23 (UTC ~18:40-20:20).

## What ran (koopa; one heavy process at a time, each under systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 nice -n 10; HOME/TMPDIR under /dev/shm/orc-review-388r2, deleted at the end)
- w/ = clone of ../t (trial 877896d). Reviewer tests added only in w/tests (copies in ev/): test_rev388r2.py (stub backend), test_rev388r2_bd.py (real bd, ORCHESTRA_BD_BIN=/home/james/beads-team-pilot/runtime/bin/bd). Reviewer servers bound 127.0.0.1:29700-29799.
- ev/base1.txt: test_rooms test_rooms_http test_rooms_items_http test_key_projects test_record_json test_capability_wiring -> Ran 170, OK (skipped=2)
- ev/base2.txt: test_rooms_items_real_bd test_capabilities -> Ran 108, OK (skipped=3)
- ev/wiring.txt: test_capability_wiring -> Ran 21, OK; every code/test pointer of capability-proposals/rooms.items-acceptance.json resolved by an AST check (all True).
- ev/repro_stub.txt (+ repro_stub2.txt: F4 and L1 rerun after fixing the reviewer test), ev/repro_bd.txt, ev/repro_bd_r9b.txt.
- ev/mutants.txt: 28 mutants (ev/mut388r2.py, ev/mutrun.sh; M1-M16 = round 1, M12/M14 re-anchored; M14b, M17-M27 new).

## Round-1 findings
- B1 FIXED for the sequential cases (F1,F1b-F1f stub counts 0 / (1,0,1); R1 2 rows, R2 0 new proposals, R3 0 rows). Residual race -> new B-2 below.
- B2 FIXED (F2: owner 200, worker 409 final, 1 record; R8 two owners 200/409, 2 rows, 1 event; R8b contributor+owner 200/409).
- B3 FIXED (F3/F3b 403; F3d/F3e contributor on others' items 403; F3f agent credential 403 everywhere; viewer answer 200 = documented).
- B4 FIXED for title/text/answer/note/reason/record on every surface; residual: moderated item's `parent` still served (N-1).
- CI pointer FIXED. S1 FIXED (F5 all 409). S2 FIXED (M5/M6 killed by delivered real-bd suite). S3 doc true. N1 FIXED for decision row/other project/missing; closed epic and plain task accepted. N2 FIXED (F6 all "Project not found"). F7 still true (cache served as-is; accept reads segment lines, not cache).

## New findings
- B-1 second amend always 409 operation_reused (D1): rooms.py:300 item_decision_operation used for amend at http_service.py:6805.
- B-2 room archive during the native write leaves an orphan record + 409 (D2 stub (1,0,1); R9 real bd: requirement + acceptance decision; R9b: proposal 0->1). rooms_archive (http_service.py:6877) takes only the room lock, not the item-decision lock.
- S-1 a failed append followed by an amend wedges the item: retry accept 422 "Operation identity reused with a different request" forever; orphan decision with the old text stays (R10).
- N-1 moderated item's parent served in read and index.json (F4). N-2 bad parent on an item is a dead end for editors (item parent wins over the page's choice). N-3 .item-decisions.lock opened without the symlink check admin.py uses for .coordination.lock (same as project_room_lock's file_lock).

## Rerun
R=/home/james/orchestra-review-evidence/1023-388r2/rev; mkdir -p /dev/shm/orc-review-388r2/home /dev/shm/orc-review-388r2/tmp
(rev/ is read-only: clone w/ to a writable copy first and adjust run.sh paths)
$R/ev/run.sh <w> out.txt tests.test_rooms tests.test_rooms_http tests.test_rooms_items_http tests.test_key_projects tests.test_record_json tests.test_capability_wiring
$R/ev/run.sh <w> out.txt tests.test_rooms_items_real_bd tests.test_capabilities
$R/ev/run.sh <w> out.txt tests.test_rev388r2
$R/ev/run.sh <w> out.txt tests.test_rev388r2_bd.Rev388r2Bd -k test_r
mutants: /dev/shm/orc-review-388r2/m = copy of w without tests/test_rev388r2*.py; then ev/mutrun.sh
rm -rf /dev/shm/orc-review-388r2
