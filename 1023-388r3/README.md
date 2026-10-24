# Review kittrial-5bb.388 r3 (rooms slice 2: items and acceptance) - delivery 618a33e on main 3a0c5b3, trial 6932fc1

Verdict: CHANGES REQUESTED (one blocker: two records for one item after a failed append + amend).
Reviewer: coordinator-spawned adversarial reviewer, 2026-10-23 (UTC ~20:48-22:50). Focused round-3 re-review.

## What ran (koopa; one heavy process at a time under systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 nice -n 10; HOME/TMPDIR under /dev/shm/orc-review-388r3, deleted at the end)
- w/ = clone of ../t (trial 6932fc1). Only reviewer tests added in w/tests (copies in ev/). All test servers forced to 127.0.0.1:29800-29899 (ev/revrun.py patches http_service.create_server).
- ev/base1.txt: test_rooms test_rooms_http test_rooms_items_http test_key_projects test_record_json test_capability_wiring -> Ran 176, OK (skipped=2)
- ev/base2.txt: test_rooms_items_real_bd test_capabilities -> Ran 108, OK (skipped=3)
- ev/repro_stub.txt: test_rev388r3seq (r2 stub set F1-F7, D1, L1; r2's D2 disabled because it called the archive synchronously inside the held lock) + test_rev388r3new (D1b-d, D2/D2b/D2c/D2d, S1a-d, N1b, N2, N3). Ran 81, FAILED (failures=2: D1b, S1a = findings). repro_stub_n2.txt = N2 rerun after fixing reviewer sequence numbers (OK). repro_stub_n2b.txt = editor's accept-parent choice (OK).
- ev/repro_bd.txt: test_rev388r3new_bd (real bd): R9, R9b, R10, R11, R12, R13, R14, R15, R16. FAILED (failures=3: R10, R12 = findings; R9b = reviewer measurement, see repro_bd2.txt OK).
- ev/repro_bd_seq.txt: r2 real-bd set (R1, R2, R3, R5, R8, R8b, R10, R11). R8b "failure" = r2 test searched proposals for the pointer, which the listing does not carry; casey won the race this time. repro_bd_r8b_{1,2}.txt rerun with a proposal count: OK twice.
- ev/mutants.txt: 39 mutants (ev/mut388r3.py, ev/mutrun.sh), delivered tests only.

## Round-2 findings
- B-1 FIXED (D1: second amend 200; D1c concurrent amends 200/200, both lines, accept records the last; D1d amend twice then accept records TC/XC). Residual S-A below (A->B->A third amend is a silent replay).
- B-2 FIXED (D2 stub: room open during write, archive waited 1.5 s, accept 200, (1,0,1), 1 event; R9 real bd: archive 200 after 11.2 s, accept 200, 2 rows, 1 event; R9b: accept 200, 1 new proposal, 1 event). Other routes: moderation (D2b) and supersede (D2c) wait on the item lock; project archive does not (D2d -> N-A). No room unarchive/rename route exists.
- S-1 PARTLY: unchanged retry reconciles (R11: 200, same pp-*.1, 2 rows; S1b stub 1 record). But failed append -> amend -> accept now writes a SECOND record (B-A).
- N-1 FIXED (F4 every surface clean incl. parent; N1b: amend parent job id still in JSONL export -> N-B).
- N-2 FIXED (N2: missing/int/list/other-project parent 422; contributor bad parent 422, good 200; contributor on owner's item 403; contributor payload parent ignored (proposal); decision amend with parent 422; empty job list now 422; N2b editor's explicit parent wins on accept; R16 real bd decision-row/other-project/missing 422 on amend and accept).
- N-3 FIXED for lock files (N3: symlinked .item-decisions.lock -> accept 500 damaged, victim not created; symlinked .coordination.lock -> accept/post/read 500, victim not created). Symlinked project directory still followed (note).

## New findings
- B-A two records for one item. A failed room append after the native write (answer 500 "Internal error"), then amend, then accept: the accept identity now includes the content digest, so a new native record is written and the orphan stays. Real bd: R12 requirement -> pp-4k7.1 + decision pp-grd AND pp-4k7.2 + pp-dyb all carry orchestra:room/<rid>#1 (item points at .2); R10 decision item -> pp-93m "Use CSV" and pp-vza "Use TSV"; R15 contributor -> two submitted proposals; R14 A-fail/B-fail/A -> two requirement+decision pairs. http_service.py:6762-6764 (accept identity from item_content_digest), 6798-6800 (_room_item_record base=operation_id). The delivered test tests/test_rooms_items_http.py:507-534 asserts 2 requirements (line 532). docs/HTTP_DEPLOYMENT.md:4298 "One record per item" is false in this path.
- S-A amend back to an earlier amendment's text is a silent no-op: http_service.py:6852-6854 identity = digest(title,text,parent); rooms.py:1215-1224 finds the old line, replays it (200, replayed:true) with the CURRENT (other) text. D1b: A->B->A answers 200 with TB/XB, item stays B, 2 amend lines.
- N-A project archive during an accept's native write: accept 200 and the item-event is appended in an archived project (D2d). Project archive does not take the item lock; one record, answer truthful.
- N-B JSONL export keeps an amend event's parent (a job id) for a moderated item (rooms.py:1425-1427 pops title only). Not user text.
- N-C accept identity ignores the parent: after a failed append, an accept with another explicit parent gets 409 "Requirements changed" (R13) until the original parent is chosen again.
- Test gaps (survivors): M34 (editor choice on accept), M36, M37 (empty job list), M38.

## Mutants (r2 -> r3)
Same 12 survivors as r2 (M1 M2 M3 M7 M11 M12 M15 M16 M18 M25 M26 M27). M17 (re-anchored M17r3) and M21 (re-anchored M21r3) still killed. Killed as r2: M4 M5 M6 M8 M9 M10 M13 M14 M14b M19 M20 M22 M23 M24. New r3: killed M28 M29 M30 M31 M32 M33 M35; survived M34 M36 M37 M38. No regression.

## Rerun
R=/home/james/orchestra-review-evidence/1023-388r3/rev; mkdir -p /dev/shm/orc-review-388r3/home /dev/shm/orc-review-388r3/tmp
(rev/ is read-only: clone w/ to a writable copy, copy ev/test_rev388r3*.py into its tests/, adjust run.sh/mutrun.sh paths)
$R/ev/run.sh <w> out.txt tests.test_rooms tests.test_rooms_http tests.test_rooms_items_http tests.test_key_projects tests.test_record_json tests.test_capability_wiring
$R/ev/run.sh <w> out.txt tests.test_rooms_items_real_bd tests.test_capabilities
$R/ev/run.sh <w> out.txt -v tests.test_rev388r3seq tests.test_rev388r3new
$R/ev/run.sh <w> out.txt -v tests.test_rev388r3new_bd
$R/ev/run.sh <w> out.txt -v tests.test_rev388r3seq_bd -k test_r
mutants: /dev/shm/orc-review-388r3/m = copy of w without tests/test_rev388r3*.py; then ev/mutrun.sh
rm -rf /dev/shm/orc-review-388r3

## Not checked
CI (runs separately), web page JS behaviour in a browser (read only: rooms.js accept/amend forms), Windows, the office RHEL 8 route, admin room backup/restore, role demotion mid-request (M27 survives as in r2).
