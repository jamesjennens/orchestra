# Review kittrial-5bb.388 r4 (rooms slice 2: items and acceptance) - delivery d49dc1c on main 7fe1a46, trial 391493f

Verdict: CHANGES REQUESTED.
Reviewer: coordinator-spawned adversarial reviewer, 2026-10-24 UTC ~00:10-04:30. Focused round-4 re-review, lane ds-goomba-1.

## What ran (koopa; one heavy process at a time under systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 nice -n 10; HOME/TMPDIR under /dev/shm/orc-review-388r4, deleted at the end; real bd = /home/james/beads-team-pilot/runtime/bin/bd as a binary)
- w/ = clone of ../t (trial 391493f). Reviewer tests added only in w/tests (copies in ev/). Test servers forced to 127.0.0.1:29800-29899.
- ev/base1.txt: test_rooms test_rooms_http test_rooms_items_http test_key_projects test_record_json test_capability_wiring -> Ran 181, OK (skipped=2)
- ev/base2.txt: test_rooms_items_real_bd test_capabilities -> Ran 108, OK (skipped=3)
- ev/repro_r3bd_rerun.txt: round 3's real-bd set (test_rev388r3new_bd: R8bc R9 R9b R10-R16) on r4 -> Ran 20, OK.
- ev/repro_stub_r3rerun.txt: round 3's stub sets (test_rev388r3seq + test_rev388r3new, incl. D1b, F4, N1b) -> Ran 92, OK.
- ev/repro_r4bd.txt, ev/repro_r4bd_2.txt: new real-bd repros (ev/test_rev388r4_bd.py, N1-N9). _2 reruns N6-N9 after fixing reviewer keys shorter than 8 chars (422 in the first file is the reviewer's key, not the delivery).
- ev/repro_stub_r4.txt: new stub repros (ev/test_rev388r4.py: A,B,A,B; concurrent amends; amend while pending). Its test_n2_jsonl_parent_cleared FAILED on a reviewer fixture error (job_0002 is not a stub job, amend 422); N-2 is evidenced by N1b in repro_stub_r3rerun.txt instead.
- ev/mutants.txt: 48 mutants (ev/mut388r4.py built from round 3's by ev/mk_mut.py + ev/fix14b.py), delivered tests only, one at a time.

## Round-3 items
1. B-A FIXED for same-editor retries, NOT FIXED across callers. R10/R12/R14/R15 rerun real bd: one decision (R10 pp-luc), one requirement+decision pair (R12 pp-pmd.1+pp-c3s, R14 pp-044.1+pp-m9c), one proposal (R15), amend while pending 409. But the native identity is per principal (http_service.py:720-723 _result_key hashes user_id; owner_requirements 3292, invoke 729):
   - N1 alex's accept fails after the record; coordinator drew retries -> 200 and a SECOND pair: pp-o38.1+pp-u5r AND pp-o38.2+pp-cvp.
   - N4 alex (editor) fails; contributor casey accepts -> 200, proposal p-a3b6c0d793da in addition to requirement pp-ksu.1.
   - N5 casey fails (proposal p-377494d94dd2 written); alex accepts -> 200, requirement pp-0et.1 too.
   - N3 coordinator drew fails, is demoted to contributor, retries -> 200 (not refused) and writes proposal p-4250ff7559a1 beside requirement pp-6my.1. As viewer: 403.
   - N2 retry after the room was archived -> 409 archived; see item 2.
2. STUCK ITEMS: NOT FIXED (new blocker). No route clears or expires an intent (grep: only http_service.py:6788 writes one; rooms.py:337 reads it).
   - N6 native write fails (no record written): moderate 409 accepting (rooms.py:1421), reject/amend 409, supersede 409; the item text stays served to members; moderating again later still 409. The only exit is to accept it, i.e. write the text into the tracker.
   - N7 contributor casey's accept fails: owner moderate 409 accepting, owner reject 409 -> a contributor's intent blocks the owner's moderation.
   - N2 failed accept, then room archive 200 (archive ignores pending intents): retry 409 archived, moderate item 409 accepting, reject 409 archived; message moderation in the same room 201 (control). No unarchive route: permanent, abusive text served.
   - N8b after a failed append, contributor drew posts a message with Idempotency-Key = the accept identity (computable: rid, seq, sha256(title, text)): retry accept 409 final "already decided" (item is proposed), moderate/reject/amend/supersede 409 accepting -> permanently stuck by a contributor.
   - N8 before any accept, drew posts a message keyed 'intent-'+identity: accept 409 operation_reused; amend unblocks (S).
   - N9 failed accept with explicit parent job2: retry without parent 409 "Requirements changed", moderate 409; only a retry naming job2 (not recorded in the intent line) unblocks.
   - The intent line carries no user text; read/cache/markdown skip it; JSONL keeps it (author only).
3. S-A FIXED. D1/D1b: A,B,A applies (200, replayed False, item TA). SABAB: TA,TB,TA,TB each applied, 4 lines; unchanged fresh key replays (200 replayed True). SCC: owner+coord same text concurrently -> one line (second replayed), worker's other text applies. SP: amend while pending 409 accepting, retry accept 200, amend after 409 final, 1 record.
4. N-2 FIXED. N1b: moderated item, amend parent job_0001 count 0 in read/export_md/export_jsonl/index.json. F4 every surface clean.
5. Regression: none in the reruns (r3 seq/new stub 92 OK; r3 real-bd 20 OK).

## Mutants (r3 -> r4)
Survivors r4: M1 M2 M3 M7 M11 M12 M15 M16 M18 M25 M26 M27 M29 M37 M38 + new M41 (moderate ignores pending) M42 (supersede ignores pending) M46 (intent replays any identity).
Now killed (survived r3): M34, M36. Now survives (killed r3): M29 (accept identity without content).
Killed as r3: M4 M5 M6 M8 M9 M10 M13 M14 M14b(re-anchored M14br4) M17r3 M19 M20 M21r3 M22 M23 M24 M28(re-anchored M28r4) M30-M33 M35. New killed: M39 M40 M43 M44 M45 M47.

## Rerun
R=/home/james/orchestra-review-evidence/1024-388r4/rev; mkdir -p /dev/shm/orc-review-388r4/home /dev/shm/orc-review-388r4/tmp
(rev/ is read-only: clone w/ to a writable copy, copy ev/test_rev388r*.py into its tests/, adjust run.sh/mutrun.sh paths)
$R/ev/run.sh <w> out.txt -v tests.test_rev388r4_bd -k test_n
$R/ev/run.sh <w> out.txt -v tests.test_rev388r3new_bd
$R/ev/run.sh <w> out.txt -v tests.test_rev388r3seq tests.test_rev388r3new tests.test_rev388r4
mutants: /dev/shm/orc-review-388r4/m = copy of w without tests/test_rev388r*.py and .git; then ev/mutrun.sh

## Not checked
CI (runs separately), the web page in a browser (rooms.js handling of 409 accepting), Windows, the office RHEL 8 route, admin room backup/restore, a real process kill between intent and record (simulated by an injected native-write exception, N6/N7), concurrency of two different-principal retries.
