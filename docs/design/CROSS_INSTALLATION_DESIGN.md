# Coordinators and programs across installations

Status: **design only**, kittrial-5bb.448. No cross-installation transport, key store,
importer or permission change is implemented by this note. Plan VERIFIED in coordinator
comment `01a189cf-ab8a-75b1-ae74-b2f12f5abd7b` (2026-10-29), including Q1–Q3,
the private GitHub mailbox option and the two ownership cases. An independent security
review and separate implementation authorization remain prerequisites. R2 addresses review
`01a189df-722f-75c4-a2ee-99a74b57a943`: the route is retained; crypto, credential
custody, note projection and manual freshness are made explicit.

## 1. One-page decision summary

**Recommend a dedicated private GitHub repository's releases as a mailbox for signed,
recipient-encrypted, selectively exported envelopes.** Each installation uploads its
outgoing files and pulls incoming ones over its approved outbound connection; neither
exposes an Orchestra service to the other. Start with person-approved transfers, then
add scheduled collection only after the same verifier and recovery rules pass review.
Manual carriage is the recovery path for the same files, not a second authority model.
Each manual exchange requires the owner's recorded live-contact approval for that
exchange on both sides; a slowly carried file cannot pass the normal ten-minute check.
This changes the provisional human-carried recommendation because the coordinator
confirms the office already downloads private release assets with `gh` (§2, E1).
It does **not** establish permission or connectivity for uploads, unattended credentials,
a new repository, or confidential data egress. If those checks fail, stop deployment;
use an explicitly approved manual channel, or exchange nothing (§3).

There are three products of exchange: a note to another project's coordinator role,
a pair of owner-approved declarations connecting a remote area to a program, and an
explicitly selected interface-register snapshot. None grants membership or a remote
read. A program spanning installations is a locally readable collection of deliberately
shared evidence, **not** a distributed database or proof that every remote area was read.
The existing local program model remains useful; its authority is not federated (§§2, 4).

**Home and office, one owner:** the same person approves each installation's separate
identity and scoped exchange; office policy still applies. Keep separate keys, credentials,
replay state and backups. Ownership by the same person does not merge trust domains.
**Another person's installation, two owners:** each owner independently pins the other
installation's public keys and approves the exact project pair, purposes, recipients and
retention. Either can pause exchange. No first-message trust or global directory (§5).

Private is necessary but insufficient. Named repository readers, writers, administrators,
the hosting provider and approved backup paths form the transport exposure boundary.
Use recipient encryption as well as signatures; keep titles/filenames opaque. No private
material in public repositories, even encrypted. Never export other projects, member lists,
private records, credentials or entire runtime snapshots. Local owner approval names the
small, intentionally shared payload; signing is not permission to disclose it (§§3, 6).

Signatures prove possession of a pinned installation key. They do not prove the sender
still has a remote coordinator grant, or that a snapshot is complete or current. Both
sides check local authority, sharing consent and known revocations on every new action.
A recent reply to a one-use check sent by the receiving installation limits how long
remote evidence may be treated as current; stale, unknown or
unreliable freshness means **no new actionable import**. Disconnection cannot promise
instant revocation. Historical material remains labelled historical, never silently live (§7).

Delivery, import, read and settlement are different states. Retries cannot duplicate a
note; reused identities with different bytes are conflicts. Replay and trust state survive
restarts and coordinated backups. Restore uncertainty stops imports until owner-led
resynchronization. Remote text is data, never an executable instruction or automatic
decision. Wake local coordinators by pointer only (§§7–9).

Build in five reviewable slices: trust and verifier; manual note exchange; restricted
private-release transport; remote program declarations; selected interface snapshots.
No slice may relax local access checks. The future adversarial matrix in §11 is a design
acceptance specification, not tests run for this note. The seven existing regression-test modules are delivery checks only. No runtime implementation or deployment is authorized here (§10).

## 2. Evidence, scope and present boundaries

Citations use **M** = base main `7f3d08bd6c8c361de2f77ae466a3b021829aeabd`,
**L** = approved .446 `b4fcd534101f3c1a50c8cd1e0b0ceb857e66b0a7`.
An L citation is branch evidence, not a claim it is in M or deployed. **N** is the
.447 verified plan, comment `01a1896e-ce57-762d-b30b-e90ffc901825`, with its
coordinator amendments `01a1896f-d5c5-7401-9957-5b6f8d04b0a2`. N is planned
behavior, not completed runtime evidence. Proposed rules below are this note's design.

| Evidence | What it supports and its limit |
| --- | --- |
| M `http_authority.py:366`, `http_authority.py:385`, `http_authority.py:1022` | Roles/capabilities and the session project's membership check; missing and inaccessible projects give 404. These are local checks. |
| M `http_authority.py:607`, `http_authority.py:706`, `http_authority.py:843` | Coordinator grant lookup, issuer authority, and live agent editing checks. A foreign identifier is not a local credential. |
| M `docs/design/PROGRAM_DESIGN.md:63`, `:80`, `:103`, `:117` | A program is an ordinary project; bilateral links grant nothing; each project read checks the caller. Old prose at M:86–94 about who accepts is superseded by L below. |
| L `program_links.py:11`, `:81`, `:96`, `:122`; L `docs/design/PROGRAM_DESIGN.md:103` | A side counts through live owner/operator acceptance; missing acceptance or lost owner authority does not count. Coordinators propose; person owners accept by their own session, or listed operators act. Native author labels are not identity proof. |
| L `tests/program_isolation.py:1`, `:47` | Existing isolation harness compares inaccessible-area and missing-area answers without rows or counts. Extend this discipline; do not claim constant-time responses. |
| M `docs/design/PROGRAM_DESIGN.md:135`, `:178`, `:230` | Proposed project-qualified pointers, interface kind and complete-view retirement rule. They are design dependencies, not all implemented features. |
| N, record subsection and Q1–Q4 amendments | Planned notes address `{project, role: coordinator, installation: null}`; live link permits sending without read rights in B. Note text is confined to its two projects' coordinators/owners/operators; wakes carry pointers; 30 notes/hour/project pair. |
| M `docs/OFFICE_SERVICE.md:13`, `:123`, `:428` | Runtime/identities/backups are private, artifacts use an approved route, network/firewall permissions are operator matters. A release-download route is not general egress authorization. |
| E1: .448 VERIFIED comment named above | Coordinator states office can reach github.com and already downloads private-repository releases with `gh`. This is coordinator-supplied operational evidence, not a worker network probe or permission for uploads. |

Official transport references, consulted 2026-10-29: GitHub's
[release access rules](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
make release reading depend on repository read access and management on write access;
[personal-repository permissions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)
say private-repository collaborators have write access, so a personal repository does not
supply arbitrary collaborator read-only roles; and
[`gh release download`](https://cli.github.com/manual/gh_release_download) downloads assets.
These establish tool/access concepts, not the office's complete network allowlist.
Use owner-reviewed organization repository roles when reader/writer separation is needed;
verify the actual account and token configuration before deployment. GitHub also offers
[immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
which protect published assets and tags against modification. They are an option for
mailbox integrity, not sender authentication, freshness, delivery or permission to retain
private files indefinitely. Evaluate their retention/deletion constraints before enabling.

Search basis: inspected M's `docs/design/PROGRAM_DESIGN.md`, authority modules and
repository searches for `installation_id`, `federation`, `mailbox`, `ed25519`, `signature`
and `hmac`; inspected L's program links and N. Hits include SSH keys, HMAC cursors and
native bd federation packaging notes, which are not evidence of an Orchestra peer-envelope
verifier. This note claims none of them implements this protocol. Capability lookup
“cross installation programs coordinator notes” in this checkout and reference lookup
“office network installation” missed; record the resulting design and E1 provenance in
the catalogs at delivery. No inference of absence solely from a lookup miss.

## 3. Options and the office network

All options require both owners' permission to disclose selected material. A network
connection does not supply that permission. No option may tunnel around office policy.

| Route | Network requirement / office fit | Readers and leakage | Revocation, recovery and operator burden |
| --- | --- | --- | --- |
| One shared installation, projects per person | Every participant must reach that service over an approved route. GitHub-only reachability does not establish this. | Local per-project controls apply, but one operator/host boundary holds everyone's state. No peer synchronization; shared hosting may be unacceptable to another owner. | Ordinary local membership revocation; one service/backup regime. Simplest if all owners knowingly accept that common boundary; does not meet the separate-installation case by itself. |
| Dedicated relay/mailbox service | Both sides can use outbound TLS to an agreed service; no inbound peer port. Requires a new reachable endpoint, identities, storage and support. | Relay sees routing/size/timing and plaintext unless envelopes are encrypted. Scoped mailboxes still need application verification. | Can delay/drop/replay; availability and cleanup become a new service obligation. Not justified as the first additional service when GitHub delivery is already used. |
| Signed exports/imports carried by people | No service-to-service connection; an approved removable-file or document-transfer route must exist. Offline works only as far as freshness checks allow. | The carrier and file copies see metadata; encryption still required for private payloads. Copying an entire backup is forbidden. | High manual approval, receipt and reconciliation cost; delayed revocation and human mistakes. Useful first verifier exercise and outage recovery, unsuitable as the normal recurring conversation mechanism if the private mailbox passes policy checks. |
| Pull of a published read-only view | Reader reaches an approved publisher; no sender writes to the reader. Public GitHub can carry expressly public snapshots only. A private publisher needs approved authenticated access. | Publication exposes the selected view to every permitted reader; public publication is irrevocably public. A view is not a conversation or acceptance authority. | Need signed version/freshness metadata; rollback/withholding remain possible. Low reader burden; no acknowledgement/reply without adding another channel. Good later distribution pattern for intentionally public interfaces. |
| **Private GitHub repository/releases as signed-envelope mailbox** | Existing private download path is evidence for one direction only. Test approved upload, listing, API/asset redirects, authentication and proxy/CA handling with synthetic files on the actual office host. No inbound Orchestra listener. | Named people only; repository roles are not per-project Orchestra roles. Provider/admins and authorized repository parties see metadata. Opaque filenames plus recipient encryption reduce content exposure, not traffic analysis. | Repository/token access can be removed; copies and earlier reads cannot be recalled. Mutable releases allow writers to replace/delete assets; immutable releases are an optional stronger publication boundary. In either mode verification and durable local receipts are essential. Avoid source-history retention of envelopes; release assets still require explicit retention/backup review. Reuses a familiar artifact route but adds credential, quota, queue and recovery operations. **Recommended, conditional on the checks below.** |

Prefer a dedicated mailbox per owner pair and sensitivity boundary, not the public kit
repository, a general shared engineering repository, or one global mailbox for all peers.
More than two owners need separate approved channels; never relay another pair's material.
Use a fine-grained token or GitHub App token limited to the one mailbox repository,
Contents read/write only, with an expiry; mode 0600, readable only by the transport
service user. Agent and coordinator processes cannot read it or the signing/encryption
keys; never use the owner's general token. Separate service identities and filesystem
permissions must enforce this even when an agent can run local commands; a naming
convention or mode 0600 under the same user is not separation. The signing service keeps
its keys separate from transport credentials and exposes only authorized signing/import
operations. Private keys and credentials never enter a repository, release asset, command
transcript or note. This design neither provisions nor reuses the office release credential.

On the mailbox repository: Actions, Pages, webhooks, deploy keys and forking off;
two-factor authentication required for every named person; owners review collaborator changes. Use an
owner-approved organization configuration that can enforce these controls; inability to
enforce them is a stop condition, not permission to weaken them. In the two-owner case,
the office-side person owner administers the dedicated repository under this agreement;
the other owner approves its access list/changes and treats that repository and its
administrator as an untrusted transport. Each side keeps its own credentials and trust
pins. A compromised repository administrator still cannot create a valid peer signature.
One owner of home and office administers it in the same separate transport role.

**Deployment gate:** owners approve data classification, named repository access and
retention; office operator approves outbound writes, credential storage, proxy/CA and all
actual download endpoints; synthetic end-to-end two-way transfer passes; reviewed crypto
is packaged for supported clients; restore/revocation drills pass. Failure leaves the mailbox
disabled. Do not fall back to public hosting, disable TLS checks, widen a firewall, or place
data in a different service automatically. Manual carriage needs its own explicit approval.

## 4. Use cases and names

**Notes.** A coordinator in A selects B's coordinator role, writes plain text and previews
the exact shared content. Local live coordinator authority and a scoped bilateral exchange
agreement are checked before export. For the first remote-note slice, the bilateral agreement includes both owners' explicit
program/area (or common-program sibling) relationship declarations and their exact hashes;
absence or withdrawal blocks sending. This is a versioned remote agreement, not an
alternative path around N's local live-link check. Slice 4 later adds its program view,
not the first permission to send arbitrary remote notes.
No read rights in B are required or acquired, matching
N's amended send rule. B imports as remote data visible only to its authorized coordinator
readers; an outgoing copy is visible only to A's equivalent readers. B sends a distinct signed
reply or acknowledgement. Third-project members and general program viewers see no note
text or existence, counts or thread metadata unless separately permitted for those two
projects. Owners/operators' administrative access remains local, as in N; it never becomes
an implicit remote read right.

**Programs.** A program at A and an area at B each have an ordinary local project. Each
owner approves its own side and the scope of the evidence it exports. A's view may show
“remote area, declarations observed at T; verified snapshot, freshness status X.” It cannot
say “live linked” using only old exports. Same-installation .446 keys have canonical-name
constraints (L `program_links.py:23`, `:25`); do not put a new installation-qualified id into
those keys or silently reinterpret their statement grammar. A later versioned remote-link
record binds the two local declarations and their content hashes, owner acceptance evidence,
installation/project pair and exchange-agreement epoch. Withdrawal on either side disables
new actions when learned; loss of freshness disables them even without a withdrawal.
Sibling areas may exchange notes only if both owners explicitly enable that peer pair;
a shared program is not a transit or disclosure permission.

**Interface register.** A selects exact, accepted interface revisions and permitted schema
bytes to publish to B. Export only the agreed closed projection: key/revision, agreed short
meaning, public-or-approved schema digest/bytes, compatibility claim, status and provenance.
Omit private repo paths/URLs, free-form evidence, member/provider lists and source history
unless each particular field is separately classified and explicitly included in a future
schema. Links are displayed as opaque provenance, never fetched automatically. B views
“remote observed revision,” not locally accepted interface truth. Unknown/newer schemas
remain unsupported, not silently downgraded. Never conclude that an interface has no
consumers or can be retired from an incomplete remote snapshot; M's complete-view rule
(`docs/design/PROGRAM_DESIGN.md:230`) still requires complete authorized evidence or a
new local owner decision. Absence is “not shared/unknown,” not “does not exist.”

Names are proposed structured tuples, not URLs to dereference:
`{installation: <random stable installation id>, project: <exact local project id>}`.
Use random 128-bit installation ids assigned at initialization; labels like “home” are
editable display data. Key rotation preserves installation id; a clone/new independent
installation gets a new id. Two projects named `platform` at different installations differ.
No global search/discovery, remote membership federation or automatic cross-installation
pointer resolution. A deliberate snapshot read is the remote interface-register read.

N's `to.installation: null` continues to mean **local to the source installation**.
A remote envelope requires an explicit non-null source and destination installation;
never infer “local” at the receiver from null. Export only: note id, thread id, sender role, an owner-approved display label, created
time, and text. Local account ids, agent ids and any other N fields are omitted. The
receiver stores the carrier beside a local reference. Remote dedup/thread ids use
`(source installation, note id)`.
A reply binds the original source/destination pair and thread. It cannot redirect a thread
or add a third party; forwarding needs a new approved exchange and explicit disclosure.
Remote display names/user labels are source assertions, not local account lookups.

## 5. Identity, consent and keys

Threat model: hostile or compromised mailbox writer, curious repository reader, forged
sender labels, stale/reordered messages, malicious payloads and an honest-but-offline peer.
Local installation administrators and the signing process can lie or leak plaintext; a
compromised trusted installation is beyond what its own signature can distinguish. Limit
blast radius with per-peer/project consent, encryption, revocation and human review.

Each installation has a dedicated signing key and separate recipient-encryption key,
never reused SSH/login keys or a shared HMAC secret. Use libsodium through PyNaCl: crypto_sign (Ed25519) detached signatures and
crypto_box_seal (X25519 sealed box) to the pinned recipient key. Slice 1 must prove it
packages for Python 3.6 on RHEL 8 (the office cannot pip from the internet) and for every
client, or choose another named, reviewed format (age plus minisign) by a reviewed
decision. No other primitive and no custom combination. The detached signature is needed
for sender authentication: a sealed box alone does not identify its sender (see libsodium's
[sealed-box documentation](https://libsodium.gitbook.io/doc/public-key_cryptography/sealed_boxes)).
Do not assume a currently published PyNaCl release supports that old interpreter; portable,
offline installation and maintained-version suitability must be demonstrated, not inferred.
Exact dependency versions, byte encoding and cross-platform vectors remain a blocking
slice-1 qualification of this named choice. No runtime dependency is added by this note.

A person owner on each side checks the other's installation id and public-key fingerprints
through an independently authenticated channel, not a file supplied by the mailbox alone.
Record local approval of the pinned signing/encryption keys, peer owner identity and a
bilateral agreement: exact project pair, allowed kinds/direction, field allowlist,
recipients, retention, quotas, freshness policy and agreement epoch. Approvals on both
sides are required, even if the same person owns home and office. Another person's owner
must give their own approval; one owner's account/credential is never copied to the other.
Record the owner act in each installation's local authority system; a signature naming an
owner is not a substitute. Local operators manage key custody under those approvals.

An installation signing service checks the sender's live local coordinator authority at
export and before releasing queued files, plus local owner consent and selected payload.
It signs an assertion of that check and its time, not the raw user's claimed name. Private
signing keys are restricted to that local service/operator; agents receive no signing key.
B verifies the pinned installation signature and agreement, then applies B's own current
local authorization and review. It trusts A's assertion within the owner's explicitly
accepted scope; B cannot independently authenticate A's current person or grant. No
remote principal is made a local user, operator, coordinator or member. Notes remain
untrusted data and cannot create tasks/commands, accept records or settle local work
without an authorized local action. No auto-following instructions embedded in text.

## 6. Selection, confidentiality and wire boundary

Default export is empty. Explicitly shared notes and the selected interface projection
are the sole exceptions to keeping private records local. Never export unrelated projects,
member lists, permissions, raw native exports, backups, credentials, key material, private
reference/capability/requirement records, audit text or filesystem paths. Do not recursively
expand a pointer or thread. Validate local selection under current permissions before a
preview, and recheck at release so queued content cannot outlive withdrawn consent.
Intentional text can still contain a secret: show an exact preview and require local
approval for first contacts and sensitive content; no scanner can promise to identify all
secrets. The sender is responsible for classification; the receiver cannot undo disclosure.

The named signature signs exact bytes containing a closed versioned manifest and payload
digest, under a
domain separator specific to this protocol. Manifest fields: version/kind; random message
id; explicit source/destination installation+project; agreement id/epoch; signing and
recipient key ids; per-direction stream epoch and sequence; original note/thread ids when
applicable; creation/expiry; receiver status-challenge reference; payload byte length and
SHA-256. All fields affecting routing, expiry, identity, content or authority are signed.
After signature verification, unknown fields/kinds/algorithms, duplicate JSON keys,
noncanonical bytes, invalid UTF-8,
nonfinite numbers or inconsistent digests are refused, not normalized and then trusted.
Verify the signature over the exact received bytes, then parse them; never re-serialise
or canonicalise before verifying. Use the existing strict JSON discipline as precedent (M `record_json.py:1`), not a claim
that it is already a signature container.

Sign the canonical plaintext manifest+payload binding; place that signed object wholly
inside authenticated encryption to the pinned recipient key. The external transport
wrapper has only a random opaque id, bounded format/version and ciphertext length.
Sender/project ids, subject, note text and filenames from users do not go in release
titles, tags or asset filenames. Verify bounded container/recipient integrity and signature
before trusting inner fields. No raw key/algorithm negotiation from an incoming file.
The sender emits the canonical signed bytes; the receiver verifies those bytes before
parsing and then validates their canonical encoding without trusting a re-serialized copy.
Actual byte framing (detached signature plus signed bytes within the sealed box) must be
frozen with cross-implementation vectors in slice 1; no alternative primitive is implicit.

Proposed initial bounds: one object per file, at most 1 MiB encrypted and 512 KiB decoded,
no compression, archives, attachments or executable content; note text at most 4,000
characters and remote interface snapshot at most 50 entries within the byte bound.
No truncation of signed content: reject oversize. At most 30 new notes/hour/directed
project pair (consistent with N), at most 1,000 unsettled imports per local target project,
and a separate bounded invalid-file quarantine. Stop accepting new material at capacity;
never drop unsettled items or replay receipts silently. Signature/decrypt attempts also
have per-peer and global work quotas so a transport attacker cannot monopolize the service.
Defaults may be lowered by owners; raising them needs a reviewed policy revision.

The transport handles only opaque bytes in an owned staging directory. Never execute
`gh` via user-supplied shell text or extract a remote pathname. Allowlisted repository and
asset ids come from local policy. Downloads use bounded size/time and a checked regular
file; no symlink following or replacement of arbitrary files. Validate the file with the
same importer whichever transport carried it. TLS/proxy/CA checks remain enabled.

## 7. Replay, freshness and revocation

A transactional import ledger keys each message by source installation, agreement epoch,
stream epoch and message id, recording digest/sequence, result and receipt. Exact repeats
return the original result without a second note or wake. Same identity or sequence with
different bytes is a conflict, not an update. Sequence allocation and export journal are
atomic before publication; reserve per directed project pair/kind to avoid cross-project
traffic disclosure. Reordering or a gap is held pending, not skipped. A signed, owner-
approved gap resolution names missing sequence/digests before advancing; it does not
reset replay protection. Receiver rejection does not advance a stream blindly. Sender
keeps immutable outbound bytes and retries them; a new semantic action needs a new id.

**Freshness is explicit, not inferred from “latest” release, repository timestamps or
successful download.** B issues a one-use random challenge for an approved pair. A's
service answers it with signed status naming the agreement epoch, current allowed keys,
local consent/link status, highest stream position and a short validity interval, after
checking its local authority. Control-plane challenges, status responses, revocations and import receipts have their
own closed kinds and tight quotas. They authenticate using already pinned keys and the
local agreement without first needing a fresh status lease; otherwise bootstrap and
revocation could deadlock. They cannot carry note text, create inbox work or widen trust.
A status cannot enlarge B's local allowlist. B stores challenge
issuance locally, verifies the response and consumes it; it is not reusable after restart or
for another agreement. Proposed default: challenge-response received within 10 minutes,
actionable validity at most one hour from B's challenge issuance, and both operators attest
a reliable UTC clock (allowed skew two minutes). Forward or backward clock anomalies,
unknown time, expired status or missing status suspend new actionable imports/exports.
Owner policy may shorten these bounds; removing them is not an offline fallback.

Export is symmetric: A must also obtain a fresh one-use check of B's current trust,
recipient key and scoped consent before signing/releasing new actionable material. A
recent status A issued about itself is not evidence that B is ready or still consents.
A carrier may transport challenge and response files too, but a long offline round trip
cannot pass those bounds: import only into labelled non-actionable quarantine. A receiving
owner can establish fresh trust through a separately authenticated live contact and record
a new scoped epoch/challenge approval. **Every manual note exchange requires this
recorded live-contact epoch on both sides**, binding the exact sender/recipient, selected
file digests, current keys/consent and a one-use authorization. Both local owners record
that fresh live contact before release/import; the approval expires within the same
one-hour bound, and a delayed file needs a new contact. It does not waive scope, replay
or revoked-key checks, and does not turn other old files fresh. With two owners each
records their own side; one person owning both records both sides independently. Public read-only snapshots may be displayed as historical without this
handshake, but never drive a new acceptance, wake or actionable program state. Historical
private material still requires valid local read permission and is withheld if known
compromised; quarantine is not an inbox or an agent prompt.

Even a recent signed response cannot prove a remote owner did not revoke one second
later. Owners accept a bounded freshness window or disable exchange. Local revocation or
a received remote revocation takes effect immediately for new actions regardless of any
remaining lease. If that uncertainty is unacceptable, require synchronous independently
confirmed owner approval per transfer or exchange nothing. Do not claim instantaneous
cross-installation revocation from asynchronous files.

Revocation has three distinct acts: stop local exchange/key trust, remove transport access,
and send the other owner a signed revocation plus independent urgent contact. The other
side records its local cutoff and freezes new imports and unsent queues. Signed revocation
is advisory until verified against already pinned authority; a compromised key cannot
approve its own replacement or reset a cutoff. Legitimate rotation needs fresh person-owner
pinning on both sides, new key ids and agreement epoch; an old-key cross-signature is only
supporting evidence. Retain retired verification keys for historical audit, never new trust.
After suspected compromise, quarantine pending/unsettled material, label affected history
and revalidate any local decision separately. A stolen signing key may have signed false
past timestamps; do not grandfather new arrivals merely because they claim to predate
revocation. Previously disclosed plaintext, downloaded copies and remote backups cannot
be recalled. Removing repository access alone is neither key revocation nor erasure.

## 8. Audit, delivery state, restore and retention

Mailbox visibility is not delivery. Sender states: approved/queued, published, receiver-
imported, and settled. Receiver states: quarantined/refused, imported/unsettled, replied
or noted. A signed import receipt identifies message digest and outcome; a signed semantic
reply or noted receipt is separate. Lost receipt means “unknown, retry exact bytes,” not
“send a new note” or “assume read.” A status response covers challenges; a receipt cannot
serve as a status response or another project's acknowledgement. A remote reply is displayed as incoming data; it does not settle even the matching local
note automatically. Settlement needs an explicit local coordinator action on that note
after reviewing the reply or noted receipt. Only successfully imported new notes
wake authorized local coordinator agents, once, by local pointer; text never enters next,
wait fingerprints, audit reasons or prompts automatically (N).

Audit records are bounded metadata: local actor/approval reference, peer/key/agreement,
message digest/id/sequence, time, transport attempt and decision reason codes; no text,
plaintext schema or credential. Local authenticated project readers can see only their
project's authorized note history; owner/operator security audit access is explicitly local.
Unauthorized/missing peer/project refusals use the same external result with no names,
counts or body excerpts. Detailed local diagnostics are access-controlled and rate-limited;
at most one repeated refusal aggregate per peer/window, not an event per malicious retry.
Availability, timing and traffic size are not hidden; do not claim constant-time secrecy.

Replay receipts, outbound allocations, consumed challenges, agreement/key epochs,
revocations and inbox/outbox state form one backup/restore unit. Payloads follow approved
private retention, not Git source history. Keep small replay tombstones for the agreement's
life; storage exhaustion stops imports rather than pruning protection. Closing an agreement
permanently refuses its epoch, so old bodies/receipts can then be deleted under policy
without making it replayable. Backups remain private and separately access controlled,
following M `docs/OFFICE_SERVICE.md:263` as the local backup precedent, not claiming this
new state is already backed up.

A restored copy has possibly rolled-back replay/consent state. Disable send/import on
restore; never run original and restored signer concurrently. Owners compare both peers'
high-water digests/epochs and known revocations through the authenticated approval route,
record a fresh recovery epoch, and resume only after both sides agree. Unresolved sequence
or missing receipt stays unknown/quarantined. A lost peer or unavailable independent trust
record means continued suspension. Restoring the same disk image is not a new identity;
a deliberate fork gets new id/keys and new approvals. Remote replay state cannot substitute
for a missing local record of revoked consent. Test restore from before an already imported
message and from before a key revocation (§11).

Both owners approve retention before publication: working mailbox files scheduled for removal only
after receipt plus the agreed recovery interval (immutable-release retention constraints
must fit this policy or that option is not enabled), with bounded unsettled backlog alerts;
settled local text pruned on schedule without losing replay tombstones. Asset removal is
not proof that provider backups or a former reader's copy are erased. Policy requiring
stronger guarantees can rule out the GitHub route. No private content in public repositories
or kit bundles, including examples and test fixtures; use synthetic examples only.

## 9. Local reads and agent handling

Keep all existing per-project authorization checks. A peer cannot query B's members,
private records, task counts or projects through the mailbox. Remote views show only
previously approved, received projections under B's own live read permissions. Cached
plaintext is never shared across callers without the same checks. Shared program membership
is not permission to read private notes, remote schemas or local hidden areas.

Display source installation/project, verification/freshness status and observation time
beside remote data. Distinguish “not shared,” “stale,” “unknown format,” “withdrawn” and
“verified as observed”; none means a live remote lookup succeeded. Render text escaped,
with bidi/control handling and length bounds consistent with local plain-text rules.
Do not load URLs, schemas, repository paths, images or instructions embedded in it.
Program suggestions and notes need ordinary local review to become tasks or decisions;
remote settlement cannot accept a local requirement or change an agent's authority.

A note's original N shape is not mutated to pretend that a remote user id exists locally.
The carrier supplies only the §4 allowlisted note projection, signed transport provenance
and a local imported-record reference; no full local N record or account/agent id crosses.
Local N's settle/read/wake rules are dependencies to reconcile before slice 2, not code
that this task modifies. A person/agent losing local coordinator authority immediately
loses the remote inbox read and wake, even if their old session cached a pointer. Pending
work is visible only to current authorized coordinators. Operators can pause a peer
without dropping history; failed transport surfaces “delivery unknown,” not silent success.

## 10. Slice plan and owner decisions

Each slice needs a separate task, VERIFIED implementation plan, named tests, independent
security review and exact-source evidence. This note authorizes none of those builds.

| Slice | Bounded result / dependencies | Exit gate |
| --- | --- | --- |
| 1. Identity, agreement and strict file verifier | Private local identity/pin storage, owner approval records, named PyNaCl/libsodium signature and sealed-box container, strict versioned schemas, challenge/freshness and durable replay ledger. No network. | Portable supported-client vectors, negative/fuzz bounds, key/restore drills and crypto packaging review; no home-grown primitives. |
| 2. Manual note exchange | Explicit preview/export/import/receipt, local inbox pointers and settlement; bilateral declaration format (project pair, relationship, owner approvals/hashes, keys and consent epoch) and manual exchange with each owner's recorded live-contact epoch for every exchange. Depends on 1 and completed/reviewed .447 semantics; .446 local owner links remain unchanged. | Both one-owner/two-owner scenarios, all local isolation/read/wake checks, no authority transfer, stale files quarantined. Useful even if automated transport never gets approved. |
| 3. Private-release mailbox | Allowlisted repository, opaque files, approved individual credentials, bounded upload/collect/retry, no inbound service; same slice-1 verifier. | Synthetic office UAT for upload/download/list/redirect/credential revocation and restore; owner data/retention decision. Human approves initial sends; automation is separately enabled only after those gates. This completes the recommended normal route. |
| 4. Remote program evidence | Versioned qualified declarations, exact acceptance/hash/epoch binding; local remote-area view labelled snapshot/stale. Depends on 1–3 and .446; no extension of its local key grammar in place. | Either owner's withdrawal or freshness loss suspends action; no unrelated project visibility, no “live” claim from snapshots. |
| 5. Interface projections | Selected versioned interface snapshots, hash checks, explicit publisher/read policy and incomplete-coverage flags. Depends on interface-register implementation described in PROGRAM_DESIGN and 1–4, not merely that design's existence. | No automatic remote fetch/import acceptance, no retirement on incomplete data, public publication opt-in with separate owner approval. |

Decisions before runtime work: qualification of the named PyNaCl/libsodium format and
its offline portable packaging (or a reviewed age-plus-minisign replacement decision);
actual approved mailbox host/repository/roles and both transfer directions; data classification
and recipient-encryption custody; the owners' freshness/retention policy and emergency
contacts; exact remote-link/interface projection schemas after .446/.447 settle. Defaults
above are proposed bounds, not established deployment facts. Any inability to approve these
leaves exchange disabled. One owner can approve both local sides of home/office, but cannot
waive office operator policy; with two owners neither can approve for the other.

## 11. Named future acceptance and adversarial matrix

These are **future test specifications**, not implemented tests or claimed passing behavior.
The coordinator confirmed this scope in VERIFIED Q2. Every policy change below must make
its named test fail; future implementations must demonstrate that with semantic mutations.
Use synthetic identities/projects/text, injected time and isolated installations. No fixed
calendar date checked against a real clock, no live credentials or mailbox, no module-level
POSIX-only import. Existing pins do not prove these security properties.

| Proposed test name | Break the rule / required observable failure |
| --- | --- |
| `test_both_owners_pin_exact_peer_and_scope` | One absent approval, swapped installation id/key or different project pair: cannot export/import. Same owner still records two local approvals; two owners cannot impersonate each other. Every manual exchange needs both recorded live-contact approvals; expired/different-file approvals fail. |
| `test_no_first_message_trust_or_shared_secret` | Incoming key, transport credential or old-key-only rotation attempts to establish trust: refused, no pin/grant write. Secrets absent from serialized files/logs/fixtures. |
| `test_signature_binds_recipient_kind_and_all_payload_bytes` | Change each signed field, payload byte, destination or purpose; wrong/unknown signer/container algorithm: refused before inbox/wake. Destination must be this installation and source the pinned peer, so reflecting an outbound message back to its sender fails. Re-encrypting for another recipient does not redirect the signed message. |
| `test_strict_bounded_container_and_safe_staging` | Duplicate keys, unknown fields, noncanonical/invalid UTF-8, oversize nested data, archives, hostile path/symlink: bounded refusal, no arbitrary read/write/execute or URL fetch. |
| `test_agents_cannot_read_transport_credential_or_keys` | An agent/coordinator, including one able to run local commands, tries to read the mode-0600 transport credential or signer/decryption files: denied by separate service identities. A general-owner token, nonexpiring/wrong-repo token or broader permission scope is refused by provisioning checks. |
| `test_sender_checks_receiver_freshness_before_export` | Stale/missing/revoked B status or receiver-consent/key change after preview: A releases no actionable envelope, even if A's own status is fresh. |
| `test_status_response_cannot_be_presented_as_note` | Valid signed status bytes relabelled/reflected as note: closed kind/domain/destination checks refuse it; no inbox record, wake or settlement. |
| `test_export_selection_rechecks_live_local_authority` | Remove sender grant, owner consent or permitted field after preview/queue: no release/signature. No local account/agent ids or other N fields beyond the §4 allowlist, and no member/private-record/third-project material in emitted bytes. |
| `test_peer_assertion_never_grants_local_authority` | Valid signed text requests a task, grant, acceptance or remote read: only inert note data appears, no such mutation or data disclosure. Sender does not need read access in B. |
| `test_import_isolation_matches_missing_project` | A third-project user, stale-grant agent or excluded host key reads each route/count/thread/wake: indistinguishable unauthorized/missing answer and no payload; authorized two-project coordinators can read. Extend L isolation harness. |
| `test_exact_retry_once_conflicting_id_or_sequence_refused` | Duplicate upload/restart/lost receipt creates one import/wake; same id/sequence with changed bytes conflicts. Reordering is held until explicit gap resolution, never silently advanced. |
| `test_status_challenge_is_scoped_one_use_and_time_bounded` | Replay status, cross-pair nonce, stale answer, expired status, clock jump/skew or offline peer: no new actionable import/export/wake; history is labelled and access-checked. Inject clock. |
| `test_revocation_and_rotation_override_unexpired_status` | Remove local consent/pin or learn remote cutoff; replay old status/key/backdated new arrival: suspended despite otherwise valid signature. Owner-led new pin/epoch needed; history not silently trusted. |
| `test_restore_rollback_and_parallel_clone_suspend_exchange` | Restore before receipt/revocation or run clone beside signer: cannot resume until independent high-water/consent reconciliation/new epoch; no duplicate action or sequence. |
| `test_receipt_is_not_read_settlement_or_trust_status` | Publication/import receipt alone cannot mark read/noted/replied, refresh trust or settle another pair's thread. Exact lost-receipt retry recovers original result. |
| `test_note_text_is_data_and_wake_is_pointer_only` | Text with HTML/bidi/reserved prefix/agent instructions/URLs: safely displayed or rejected by local plain-text rules; never executed/fetched or placed in wake/audit/prompt. |
| `test_rate_capacity_and_audit_bounds_fail_closed` | 31st note/hour, full unsettled store, invalid-file flood or tombstone exhaustion: bounded refusal/alerts, no silent eviction of unsettled work or replay state; repeated refusals aggregate. |
| `test_remote_link_is_snapshot_not_live_membership` | One side missing/withdrawn, stale acceptance/owner evidence or sibling without pair consent: no actionable linked state, note exchange or new visibility. Same-name projects on different installations stay distinct. |
| `test_interface_projection_never_implies_complete_coverage` | Omitted area/consumer, private schema URL, unsupported version or changed digest: unknown/incomplete/refused; cannot accept/retire interface or recursively fetch. Public projection requires explicit publication approval. |
| `test_private_mailbox_permissions_and_network_stop_conditions` | Public/wrong repository, unapproved upload/redirect, revoked transport credential or TLS failure: no fallback publication/bypass. Writers replacing/deleting assets cannot forge verification; retries remain bounded. |
| `test_retention_keeps_replay_boundary_without_claiming_erasure` | Prune settled text/release asset, exhaust ledger or close epoch: old replay stays rejected. UI/audit does not claim remote copies erased. No private content enters public artifacts. |

Delivery qualification for this design: run `tests.test_record_json`,
`tests.test_old_interpreter`, `tests.test_capability_wiring`, `tests.test_capabilities`,
`tests.test_version`, `tests.test_field_limits`, `tests.test_cli_contract`; check every
M/L file:line target at its named commit and all requested topics against this note.
Report actual Ran/OK and skips separately. No claim of runtime protocol, Windows run,
network UAT, crypto correctness, deployment or security acceptance follows from doc pins.
