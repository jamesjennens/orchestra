# Coordinator notes over confined SSH

The first version of .458 exposes the existing .447 inbox through the confined
SSH client. The web service remains its sole store and writer. This is opt-in;
with no host enrollment no adapter is started and worker waits are unchanged.
The service adapter and its end-to-end tests are Linux-only. The standalone
client, parsing and metadata validation are portable; do not require a site agent.

## Operator setup

The project's owner approves a principal as a coordinator for notes in that
project. An installation operator records that approval, on the coordination host:

```
python3 admin.py --root RUNTIME host-notes enroll PROJECT \
  --principal lane:NAME --actor OPERATOR \
  --approved-by person:OWNER --approval decision:PROJECT/APPROVAL
```

These are explicit per-project grants, not consequences of program links, key
project lists, or installation operator names. Approval fields record the
operator's attestation of the owner's approval; they do not authenticate a person
merely because their name was supplied. Only an operator with access to the local
administrative interface may enroll or revoke. Review the approval before doing so.

Use a confined SSH key bound to that principal and project, and an actor owned by
that principal in the project's session registry (docs sessions). Restart the
existing HTTP service with its existing arguments plus `--host-notes`; `--root`
must name the canonical runtime. Enrollment before startup is required for the
listener to start. Revoking the last principal disables its authority immediately;
the next startup with no grants creates no listener. No network port is added.

```
python3 admin.py --root RUNTIME host-notes revoke PROJECT \
  --principal lane:NAME --actor OPERATOR
```

Revocation is read on every operation and each wait pass (at most five seconds).
The adapter does not start agents. A stopped lane needs its owner/operator's
explicitly installed and authorized launcher, or that person must restart it.

## Client operations

Use the ordinary configured client prefix, including its project and actor:

```
b coordinator notes send --to OTHER --file note.txt --request-id STABLE-ID
b coordinator notes inbox
b coordinator notes sent
b coordinator notes reply cnote-ID --to OTHER --file reply.txt --request-id REPLY-ID
b coordinator notes noted cnote-ID --request-id ACK-ID
b coordinator notes wait --timeout 45
b coordinator notes wait --timeout 45 --since RETURNED-FINGERPRINT
```

Use the full returned `cnote-...` id. `--file` is local UTF-8 attachment transport,
never a file path opened by the server. Reuse the identical request id and payload
after any failure or uncertain response. Another payload with that id is refused.
A retained request whose note has aged out answers “already handled”, never sends
it twice. Request metadata is bounded; a full metadata set refuses new writes.

Inbox and sent take `--offset` and `--limit` (1..50). Only inbox fetch marks a
fetch receipt. A sender can see accepted, fetched and settled separately; accepted
never means read. A no-receiver warning records enrollment at acceptance, not current presence;
a later fetch/settlement receipt records subsequent progress. It is returned
only after address validation.
An absent receiver does not grant anyone permission to read on its behalf.
“No reply needed” still requires noted. A reply settles its parent; a retry of the
same noted request is reconciled. A full unread inbox refuses new acceptance.

Wait returns note pointers and a fingerprint, never text. Reissue the same bounded
wait using its returned fingerprint; a timeout leaves the baseline intact. At most
one blocking wait per principal/project and sixteen in all are admitted. Enrollment
is checked again between passes, with no lock held while sleeping. This namespace
does not change `b wait` or its worker fingerprint and polling interval.

Only coordinators of a note's two projects (including their enrolled host
principals) read text and receipts. Sending additionally requires the current
.447 program pairing. A link never grants a read in the other project. Notes are
data, not executable instructions or adopted decisions.

## Authentication, failures and limits

The private Unix socket and installation key in `RUNTIME/.host-notes/` are owned
by the service account and are private. The service first proves possession of its key, before the endpoint sends note
text. The endpoint then signs a fresh per-connection challenge and the entire
request using HMAC-SHA256. Principal and key project scope
come from the confined launch arguments, never caller JSON; the service repeats
scope, registered-actor ownership and current enrollment checks. A local process presenting an invented signature without the key is refused;
the test runs that process separately. A key-owning service-account process can
authenticate and remains within the trusted-host boundary below.
An unrestricted shell with the service account's private files remains inside the
kit's existing trusted-host boundary; confinement is what denies that access to a
worker key. Never distribute the adapter key to workers or put it in client config.
The greeting uses a server-selected challenge and response frames are not signed;
protection from a process controlling the service account is outside this boundary.

The adapter uses the existing note store and service audit.
Private enrollment files and their audit carry approval metadata only. Accepted
host send/reply/noted operations also append to the existing service audit: the
hashed request identity, project, launcher actor, operation, outcome and audit time.
They copy no note text, raw request id, delivery receipt or settlement object.
A reconciled retry adds no second audit event. The event shares the note/request
save, and a failed uncommitted save rolls all three back. Note text, fetch receipts and
request identity metadata stay in the existing service state and its existing
backup boundary. Errors are fixed sentences without note contents, receipts or
host paths. A missing service, unreadable state or damaged enrollment refuses
operations; retry with the same id after the operator repairs it. No client-side
HTTP state cache is used. A restart ends pending waits; reconnect with the same baseline. The listener
lease permits recovery of an owned private stale socket after a crash, never
replacement of a live listener or a symlink/ordinary file. Operator setup and service restart are deliberate deployment actions,
not performed by a client command.

Job alarms, agent launchers and federation are outside this version. The .458
latency figures are rollout targets, not measurements supplied by this change.

## Relationship to .457

Main `493c1a2` includes .457's HTTP blocking wait. Both transports now use
`waiting.bounded_pause`: a monotonic deadline starts before the first pass, and
another pass starts only when its worst observed read cost still fits. Sleeps are
unlocked, normally five seconds and longer when needed to keep reads near a fifth
of the waiter's time. This is a soft bound: a pass slower than earlier ones can
overrun it; reads are not interrupted. Each host pass checks enrollment and actor
ownership again, and service shutdown ends the wait. Note view/marker generation
and waiter budgets remain specific to each transport. The default worker wait's
projection, polling and CLI behavior are unchanged. The
host marker tracks the last incoming accepted note, including settled notes, so
its coordinator's own reply/noted cannot trigger an empty wake.
A dropped host connection does not immediately free its waiter slot: capacity is
released when that wait returns or the service stops. A reconnect can temporarily
receive 429 while the previous soft-deadline wait is still running.

Resource bounds: private request frames are 64 KiB, reply pages 1 MiB, enrollment
metadata 1 MiB with 200 audit entries, and durable-store preflight 32 MiB. A larger
store or page fails closed rather than being silently truncated. While the adapter
is enabled, that state limit applies to web notes as well as host notes. Web callers
now receive a 503 instead of a generic 500 for failed host-note state/enrollment
validation; damaged enrollment explicitly asks an operator to repair its metadata.

The fresh durable-state comparison still reads/parses the whole file on every
pass. This version retains the bounded check rather than trusting a cached copy
after the durable file becomes unreadable or changes. No large-state performance
optimization is claimed. For an installation already above this bound, leave the
adapter disabled until its storage validation is revised; that rollout/restart is
an operator action. Link reads through the tracker run outside the enrollment and
web state locks; only live authorization, in-memory checks and the atomic effect
run under those locks.

## Request retry window

Request identities are scoped by principal and project and retained for 24 hours.
The same id and payload reconcile to the accepted result during that window;
retries do not extend its age. If its note has already been evicted, the retry is
410 and cannot silently send another note. After the window expires, the identity
may be treated as a new operation: inspect the inbox/sent state before retrying an
old uncertain write, and use a fresh id for a new intention.

The journal keeps at most 10,000 retained identities across the installation.
Expired entries are pruned on host write attempts. If all slots are still within
their window, a new write receives a distinct 503 naming the 24-hour retry window;
no change is accepted, but retained matching retries and reads still work. Once an
entry ages out, a subsequent write can proceed without an operator editing state.
Legacy entries with no timestamp receive a persisted 24-hour grace window when
R2 first saves their journal, including housekeeping when a full write is refused.
The grace timestamp survives restart. Request timestamps use the service's expiry
clock; moving the wall clock backwards does not shorten a retained window.
