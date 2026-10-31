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

There is no public adapter route, new note database or new note audit copy.
Enrollment/audit files carry approval metadata only. Note text, fetch receipts and
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

At the checked main base `eae51cb`, .457's HTTP blocking wait is not integrated.
This version uses the same bounded-pass approach in `host_notes.Manager.wait`: a
monotonic deadline, five-second unlocked waits, cancellation and live authorization
on every pass. The reusable boundary is that pass/deadline/cancellation loop and
the global waiter budget; note view/marker generation remains transport-specific.
Recheck the .457 merge before delivery and share its helper when available. The
host marker tracks the last incoming accepted note, including settled notes, so
its coordinator's own reply/noted cannot trigger an empty wake.

Resource bounds: private request frames are 64 KiB, reply pages 1 MiB, enrollment
metadata 1 MiB with 200 audit entries, and durable-store preflight 32 MiB. A larger
store or page fails closed rather than being silently truncated. The host request
identity journal keeps at most 10,000 entries and refuses new writes when full; it
never evicts an identity to accept another request. An operator must plan retention
maintenance before reaching these limits.
