# Review kittrial-5bb.388 r5 (rooms slice 2: items and acceptance), Claude lane, commit 5a346b5 on main d3f81c2, trial 59991da

Verdict: CHANGES REQUESTED (one blocker, B1).
Reviewer: coordinator-spawned adversarial reviewer, 2026-10-24 UTC ~09:50-14:40. Focused round-5 re-review.

## What ran
koopa; one heavy process at a time under systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 nice -n 10; HOME/TMPDIR under /dev/shm/orc-review-388r5 (deleted at the end); real bd = /home/james/beads-team-pilot/runtime/bin/bd as a binary; test servers on 127.0.0.1:29800-29899 via ev/revrun.py.
- w/ = clone of ../t (trial 59991da). Reviewer tests only in w/tests (test_rev388r5*.py, test_rev388r4_bd_t.py, copies of the round 3/4 reviewer files); copies in ev/.
- ev/task_comments.json: the task history, read once with the kit as the coordinator actor (bd comments, read-only).
- ev/base1.txt: test_rooms test_rooms_http test_rooms_items_http test_rooms_backup test_key_projects test_record_json test_capability_wiring -> Ran 214, OK (skipped=2)
- ev/base2.txt: test_rooms_items_real_bd test_capabilities -> Ran 112, OK (skipped=3)
- ev/repro_r3bd_on_r5.txt: round 3 real-bd set -> Ran 24, OK (skipped=3)
- ev/repro_r4bd_on_r5.txt + _2.txt: round 4 real-bd N1-N9 (prints). 7 tests hit the harness's 20 s HTTP timeout in setUp under load in the first file; rerun with a 180 s client timeout (test_rev388r4_bd_t) -> Ran 7, OK.
- ev/repro_stub_r3r4_on_r5.txt: round 3/4 stub sets -> Ran 277, FAILED (failures=1): test_n2_jsonl_parent_cleared, the same reviewer-fixture failure as in round 4 (amend 422 on job_0002), not the delivery.
- ev/repro_r5bd.txt, _2.txt, _3.txt: new real-bd repros (ev/test_rev388r5_bd.py, V1-V15). First file: 7 setUp timeouts (load) and V10 used a 7-char key (422, reviewer error); _2 reruns those 8 -> Ran 8, OK; _3 times V15 -> Ran 1, OK.
- ev/repro_stub_r5.txt: new stub repros (ev/test_rev388r5.py W1-W3) -> Ran 3, OK.
- ev/mutants.txt: 67 mutants (ev/mut388r5.py: round 4's 48 with 8 re-anchored, plus V1-V19 for the round-5 code), delivered tests only, one at a time (fast set with -f, then real bd + capabilities for fast survivors).

## Findings
B1 (two records for one item; decisions). The caller's OWN earlier attempt is never reconciled through the journal (`earlier` excludes the caller, http_service.py:6997), and room-decision binds its journal row to the login session (requirement_http.py:884-886, guarded_write without account_identity; principal_key includes session_hash, http_authority.py:1037).
- V2b: alex's decision accept fails after the native write (pp-2gp); alex signs in again and retries -> 422 "Operation identity belongs to a different principal". The only way out is the cancel route, which answers records ['pp-2gp'], record_may_exist false; alex accepts afresh -> 200 and a SECOND decision pp-5rq. A project with one owner/coordinator always ends here.
- V2: alex's own retry after his journal rows are gone (deleted between attempts) -> 200 and a second decision (pp-oh9 + pp-ye8). "MISSING -> 409, never a second write" holds only for other people's attempts.
- docs/HTTP_DEPLOYMENT.md:4341-4342 ("the one way two can exist" is a cancelled UNKNOWN outcome) is false for V2b.
- Requirements are not affected (V1, V1b: one requirement; owner-requirements uses account identity).

S1 (project lock held across an endpoint read). Moderation and archive compute the cancel line's outcome (`_room_attempt_outcomes`) inside the project .coordination.lock (http_service.py:7233/7240 moderate; 7209 archive via _rooms_write 6573). For a requirement intent that calls `requirements_read` (http_service.py:6716), an endpoint read that waits on the same lock. V15: moderating an item whose requirement attempt committed took 150.1 s (endpoint timed out), blocked the project's writes for that time, then wrote records [] / record_may_exist true although pp-kfa.1 is known; V10 and V11 show the same cancel line. Up to five authors per intent, and every pending intent in a room on archive, multiply it. Moderation does finish (201).

S2 (journal rows not checked for principal/route). `prior_room_write` (http_service.py:3359-3373) trusts any committed row under the computed id. The SSH endpoint path accepts a caller-chosen operation_id and binds it to 'actor:<name>' in the same journal (http_authority.py:1040-1041; endpoint.py:2793). V8: casey (SSH path) plants `bd create --json` under alex's requirements.create id: alex's accept -> 422; drew's retry takes the planted row as "created", reads pp-dbe -> 404; the cancel line names casey's pp-dbe as a record the attempt wrote. V9 (decision): alex 422, drew 409; acceptance blocked until a cancel, and the next identity (-c1) is computable too. Check entry['principal'] and entry['route'] before using a row.

N1 The lane's equivalence argument for M12 does not hold: message posts take only the rooms lock (rooms_post -> _rooms_write, http_service.py:7193, 6573) and rooms._append has no lock of its own (rooms.py:1388), so an accept append without the rooms lock races a message post. The code is right; the mutant is not equivalent and has no test.
N2 M26's argument cites _rooms_actor, which refuses nothing (http_service.py:6537); the credential is refused by the rooms.post capability in _mutate. Still equivalent.
N3 New reviewer survivors: V8 (an author may cancel own intent of any kind; reachable only by a demoted former editor) and V18 (the adopt path's already-accepted check is untested; without it a retry 409s, it does not duplicate).
N4 W3: an author demoted to viewer can still cancel their own proposal intent (200). Harmless; not stated.
N5 Supersede deviation: agreed. Refusing a supersede while pending keeps a contributor from cancelling an owner's in-flight acceptance; moderation, archive and the cancel route are the exits.

## Items
1. N1/N3/N4/N5 FIXED. N1 one requirement+decision (drew adopts), V5 events 1; N3 demoted 403, viewer 403; N4 casey 403, one record; N5 alex 409, one proposal; V7/V7b two callers at once: 200 + 409 final, one record.
2. Journal reconcile PARTLY. Committed adopted (V5); unknown 409 (V3); missing 409 for another editor (V4); planted pointer text not adopted (lane test in base2). Not for the caller's own attempt (B1); planted journal rows are matched (S2). Attempt lines: written only for an editor completing a requirement/decision intent or the proposal's author, after live checks; refused retries add none (V13). Lookup cost bounded (<=5 authors x <=2 point reads + 1 read).
3. Stuck items FIXED. N2 archive 200 cancels, item moderation then 201; N6/N7 moderate 201; N8 422; N8b moderate 201; N9 retry without parent 200 (intent parent); V10 cancel line names the identity, retry 409 moderated, module accept of the cancelled identity 409 cancelled; V11 archive cancels both intents; V12 rights: agent 403, viewer 403, non-author contributor 403, author own proposal 200, contributor 403, coordinator 200, again 409; W2 agent with coordinator grant 403, outsider 404; V14 room-/intent- keys 422 on messages, via X-Request-Id too. Cost: S1.
4. Supersede deviation: acceptable (N5).
5. Regression: none (r3 bd 24 OK; r4 stub failure identical to round 4; r4 bd N-set behaves as fixed).

## Mutants (r4 -> r5): 67 run, 53 killed, 14 survived
Survive (as r4): M1 M2 M3 M11 M12 M15 M16 M18(r5) M25 M26 M27 M38. New reviewer survivors: V8, V18.
Now killed (survived r4): M7, M29(r5), M37, M41(r5), M42, M46. No r4-killed mutant survives.
Equivalence checked: M15 yes (CAP_COORDINATE gate), M18 yes (decidable_item 409 final), M26 yes (capability, not _rooms_actor), M38 plausible (amend id digests parent), M12 NO (N1).

## Rerun
R=/home/james/orchestra-review-evidence/1024-388r5/rev (read-only: clone w/ elsewhere, adjust paths in ev/run.sh and ev/mutrun.sh)
- ev/run.sh <w> out.txt -v tests.test_rev388r5_bd -k test_v5
- ev/run.sh <w> out.txt -v tests.test_rev388r4_bd_t -k test_n
- ev/run.sh <w> out.txt tests.test_rev388r3seq tests.test_rev388r3new tests.test_rev388r4

## Not checked
CI (lane: 40079252168 green), the web page (rooms.js on a 422 from a new-session retry), Windows, office RHEL 8, the full suite, a real process kill between intent and record (injected exceptions only), the 24 h-30 d tombstone window with a real clock (rows deleted or set unknown instead), the proposal path from a new login session, M12's race as a running test, the lane's own mut388r5.py (not reachable; own set used).
