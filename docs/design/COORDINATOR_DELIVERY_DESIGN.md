# Reaching coordinators without a relay

Design proposal for kittrial-5bb.458; checked against main
`eae51cb6206d65502c9af794ecbe42206fe2898e`. No runtime change is delivered here.

## First version: host-session notes on one installation

Ship this first, in a few days: expose .447's existing notes through the confined
SSH client, and let an enrolled host coordinator block for its incoming notes.
Keep one inbox, thread, idempotency identity and settlement history across web and
SSH. Do not create relay tasks or require a site agent for a host coordinator.

Proposed commands: `coordinator notes send --to PROJECT --file FILE --request-id ID`,
`coordinator notes inbox`, `coordinator notes sent`, `coordinator notes reply ID`,
`coordinator notes noted ID`, and `coordinator notes wait --since FINGERPRINT`.
These are proposed interfaces, not commands available today. Document sent beside
send, and say explicitly that “no reply needed” still requires “noted”.

Add a narrow, installation-local request adapter to the running notes service;
the SSH endpoint forwards only these operations. Reuse .447's validation, pairing,
limits and live service store. The adapter must not instantiate another writable
web-state cache, edit `http-state.json`, or keep a second copy of notes. An absent
service returns unavailable and preserves the request identity for retry.

Enrollment is an operator action approved by the project's owner: bind an existing
SSH key principal to the coordinator-note role **in each named project**, and to
its registered actor. The actor registry proves identity, not coordinator status;
an installation operator name or a key's project list alone grants no note role.
Authenticate the confined launcher's identity; caller JSON or a socket's OS user
alone never proves a principal. Recheck the enrollment on every read, write and wait pass. Revocation
ends the wait and returns no further pointers or text. This adapter needs its own
implementation review before rollout.

A grant in A permits A's inbox/outbox and thread replies. Sending
A→B additionally requires .447's accepted program pairing. **A link grants nothing
in B.** Note text remains visible only to the current coordinators of its two
projects, including owners; neither an unrelated host operator identity nor a
program-wide coordinator implicitly reads every area's notes. Reuse .447's equal
address refusal before exposing receiver information.

Fold .447's unsettled-note pointers into the host coordinator wait, using .457's
bounded waiting helper when available: recheck every five seconds, release locks
between passes, reuse its concurrency caps and own-write suppression. Return pointers; read text through the inbox. Preserve existing worker
wait fingerprints; version/extend only the coordinator notes view. Today's CLI
wait minimum interval is 60 seconds, so wrapping that poll cannot meet a ten-second
target. .457 is planned work, not a deployed dependency assumed by this note.

For a permitted sender, distinguish **accepted**, **fetched by a coordinator**, and
**noted/replied**. A successful write never claims a reader exists. Add a coarse
“no receiver enrolled” warning on that sender's accepted note, without receiver
names, counts or unrelated project information. Expose fetch/settlement receipts
only to that note's two coordinator roles. Keep outstanding notes within a bounded queue; refuse new acceptance when full rather
than silently lose unread notes. Enrollment reveals the outstanding inbox.

## Which relay disappears

| Recorded need | First version or later |
| --- | --- |
| (a) A comment on another project's request task wakes nobody | Send a .447 note with the task pointer. .457 covers others' comments on the recipient's project, including unassigned tasks. |
| (b) A decision went only by session message | Send the durable note first; reply or acknowledge there. A note conveys a decision pointer; it does not adopt or authorize the decision. |
| (c) One chat told another where to read | Authorized coordinators send the pointer through notes; do not make the unrelated chat an intermediary. |
| (d) Assignment cannot restart a stopped lane | Wait can wake a running harness only. The lane owner/operator must install and authorize its launcher; until then that named person restarts it. The Orchestra coordinator cannot start agents. |
| (e) SSH-only coordinators saw no accepted notes | Enroll their host principal and expose the same .447 inbox through SSH; verify end-to-end before retiring the task relay. |
| (f) No Bridge means a two-minute poll | Reuse .457 blocking waits for web agents and the host notes adapter; Bridge remains optional. |

## Later: alarms, stopped receivers and other installations

Give each job a revocable **alarm-send-only** identity for its own project, never
a person's credential. A narrow alarm
ingress may address that project's coordinator role without a cross-project
pairing. Reuse the inbox/thread/settlement machinery, with provenance “job”, a
bounded plain-text summary, correlation pointer and stable incident key; repeated
reports update/deduplicate the incident rather than consume the ordinary note
quota and drop an alarm. Alarm senders cannot read inboxes, settle alarms, or send
to arbitrary projects. Keep credentials and payload secrets out of summaries.

An alarm such as “close cannot be calculated” remains owned by the receiving
project coordinator until fixed or explicitly handed to a named escalation owner;
“noted” acknowledges receipt, not repair. Track acknowledgement and resolution
separately. Persistent pending alarms must survive service/harness restarts. A
configured, operator-run watchdog escalates overdue acknowledgements through an
Orchestra note to an explicitly designated backup coordinator/owner. An overnight
promise requires that backup's authorized, running receiver or launcher. Without
one, report the gap; do not promise that a stopped process will awaken itself.

Job alarm ingress and federation are later slices. Do not infer that an own-server project
is reachable from a local program link. Until an authenticated federation route is
separately approved, retain an explicit coordinator relay for that boundary and
record each occurrence on this task.

## Targets and the first rollout check

Proposed targets, measured from server acceptance, not promises about today's kit:

- Durable acceptance within two seconds; an active host/web receiver fetches within
  ten seconds (five-second wait passes plus transport). Measure acceptance, wake, fetch and settlement separately.
- With an installed authorized launcher: start a stopped receiver and fetch within
  sixty seconds. With none: show “no receiver enrolled” or an overdue fetch after
  sixty seconds, and assign the lane's named owner/operator to restart it.
- Later, alarms use the same ten-second active-receiver target; acknowledge within
  two minutes, including overnight, or the watchdog escalates to the agreed backup.
  Time to fix is incident-specific and is not hidden by acknowledgement.

Before removing the working relay, an owner/operator enrolls the host coordinator,
starts its foreground wait, and sends one real note each way between two permitted
projects: sender sees accepted then fetched, receiver replies or marks noted, and
restart resumes outstanding notes without an extra wake for its own reply. Also
check a missing receiver and a revoked enrollment. These are proposed rollout
checks, not tests or live measurements performed for this design delivery.

## Evidence used

- .447's checked contract: `docs/WORKER_GUIDE.md:424`; service-owned store:
  `http_auth.py:3553`; write attribution/store locking: `http_auth.py:3607`.
- Current agent-only note wake and pointer/fingerprint folding:
  `http_service.py:9803`, `http_service.py:9836`; pairing: `http_service.py:9869`.
- Host coordinator surface and principal boundary: `endpoint.py:1049` and
  `endpoint.py:1090`. Existing wait interval: `client.py:501`.
- A launcher must be supplied by its lane: `tools/wake/README.md:3`.
- .457's coordinator plan VERIFIED comment (2026-10-31 00:56 UTC): five-second
  passes, bounded waits, no held lock while sleeping, global cap and live revocation.
  .458's coordinator VERIFIED comment adds the same-installation priority and
  explicitly forbids assuming that the coordinator can start agents.
