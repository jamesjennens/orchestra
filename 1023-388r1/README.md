# Review kittrial-5bb.388 r1 (rooms slice 2: items and acceptance) - delivery 371b05d on main a816495, trial 8ee667c

Verdict: CHANGES REQUESTED. Reviewer: coordinator-spawned adversarial reviewer, 2026-10-23 (UTC ~17:00-18:00).

## What ran (koopa, one heavy process at a time, each under systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 nice -n 10; HOME/TMPDIR under /dev/shm/orc-review-388r1, now deleted)
- w/ = clone of ../t (trial 8ee667c). Reviewer test files added only in w/tests (copies in ev/): test_rev388.py (stub backend), test_rev388_bd.py (real bd, ORCHESTRA_BD_BIN=/home/james/beads-team-pilot/runtime/bin/bd). Services bound 127.0.0.1:29600-29699 in reviewer tests (delivered tests use their own port 0).
- ev/base1.txt: test_rooms test_rooms_http test_rooms_items_http test_key_projects test_record_json -> Ran 143, OK (skipped=2; node page tests, node absent on koopa)
- ev/base2.txt: test_rooms_items_real_bd test_capabilities -> Ran 106, OK (skipped=3; label-alias tests skipped by design)
- ev/repro1.txt (stub), ev/repro_bd1.txt, repro_bd2.txt, repro_bd3.txt (real bd), ev/seg388.txt (segment/index damage), ev/mutants.txt (16 mutants, ev/mut388.py + ev/mutrun.sh), ev/mut_kill_by_repro.txt.

## Findings (see final report for full text)
- B1 accept creates the tracker record BEFORE the item state/room state is checked: second accept, accept of rejected/superseded/moderated item, accept in archived room all create orphan records (real bd: second requirement + second acceptance decision; proposal with tombstone text; decision in archived room). http_service.py rooms_item_accept/_room_item_record; requirement_http.room_decision lacks room-archived check.
- B2 concurrent contributor+owner accept -> requirement AND proposal created (stub repro F2).
- B3 viewer can amend (rewrite) and reject any question (stub F3/F3b).
- B4 moderated item: amended title still served by GET room (rooms.py _item_event_view).
- S archived project: item event routes write; outsider existence oracle; untested endpoint binds (mutants M5/M6 survive); answers/notes/reasons cannot be moderated.

## Rerun
R=/home/james/orchestra-review-evidence/1023-388r1/rev; mkdir -p /dev/shm/orc-review-388r1/home /dev/shm/orc-review-388r1/tmp
$R/ev/run.sh $R/w $R/ev/base1.txt tests.test_rooms tests.test_rooms_http tests.test_rooms_items_http tests.test_key_projects tests.test_record_json
$R/ev/run.sh $R/w $R/ev/base2.txt tests.test_rooms_items_real_bd tests.test_capabilities
$R/ev/run.sh $R/w $R/ev/repro1.txt tests.test_rev388.Rev388 -k test_f
$R/ev/run.sh $R/w $R/ev/repro_bd1.txt tests.test_rev388_bd.Rev388Bd -k test_r
mutants: needs /dev/shm/orc-review-388r1/m = copy of w; then $R/ev/mutrun.sh
rm -rf /dev/shm/orc-review-388r1
