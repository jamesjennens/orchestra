# Serving delivered code over HTTP (design note)

Task kittrial-5bb.332, plan checkpoint 01a161c7 (verified 2026-10-22). Base: main
`846d3b5`. This note is the design only; no code changes with it.

The problem, from the first VS Code worker on a real project (2026-10-17): a
contribution delivered as a git bundle exists on the coordination host as a file, and a
web-only member (the office: every person and agent is web-only, the server cannot reach
the code host, nobody but the operator reads the server's files) cannot read that code at
all. Today such a reviewer is offered the summary alone. This note designs a bounded,
authenticated read that serves the exact delivered commit to a member who may review it,
with the bundle's recorded SHA-256 checked by the service and re-checked by the client.

Nothing here lets the server reach the code host. The server opens local files only.

## 1. What a reviewer can read of a delivery today

Every route below is already served by `http_service.py` on main `846d3b5`; each is
gated by the per-project live authority check (`self._project(ctx, CAP_READ)` at
`http_service.py:4739`, which calls `check_authority` at `http_service.py:616`; routes
are never anonymous by default, `http_service.py:4100`).

| Route | file:line | What it returns of a delivery |
| --- | --- | --- |
| `GET /v1/projects/{p}/tasks/{t}` | `http_service.py:8131` (CAP_READ :8133) | The task row: title, description, labels. No code. |
| `GET /v1/projects/{p}/tasks/{t}/brief` | `http_service.py:9412` (CAP_READ :9419) | Row, current checkpoint, review chain, facts. The contribution appears as its **record**: `commit`, `base_commit`, `delivery` text, `summary`. No code. |
| `GET /v1/projects/{p}/tasks/{t}/history` | `http_service.py:9859` (CAP_READ :9881) | Exact pages of comments and review records, including the contribution body (`delivery: {kind, path, sha256}` for a bundle, `review/branch` for a remote) and `attest` records. Text only. |
| `GET /v1/projects/{p}/queue` | `http_service.py:9939` (CAP_READ :9947) | The Reviews page: contributions in flight, projected from the same records. |
| `GET /v1/projects/{p}/lanes` | `http_service.py:9961` | The lane board, same records again. |
| `GET /v1/docs`, `GET /v1/docs/{name}` | `http_service.py:5777`, `:5784` | The served docs, `docs/REVIEWS.md` and `docs/ARTIFACTS.md` included. |
| `POST /v1/projects/{p}/tasks/{t}/reviews` | `http_service.py:9713` (capability at :9721-9722) | The write path (contribute, respond, request-changes, approve). Records only. |

The contribution record itself is defined in `review_workflow.py:88`
(`repository`, `commit`, `base_commit`, `delivery`, `summary`, `supersedes`) with the
delivery shape validated at `review_workflow.py:616-622`; a bundle delivery carries a
`path` and a `sha256`, a remote delivery a `remote` and `branch`. The kit itself verifies
none of it on the write path: "Remote access and bundle contents are not verified
automatically" (`docs/REVIEWS.md`, the structured-delivery section).

There is no byte-serving route at all. The artifact route is explicitly deferred:
attachment content is refused with a clean 501 (`validate_attachments`,
`http_service.py:243-291`), and `artifacts.py` / `docs/ARTIFACTS.md` is a
filesystem-only convention ("The helper does not grant access"). `admin.py
attest-delivery` (`admin.py:9990` parser, `:10787` handler) reads a local bundle for a
listed verifier on the host - never over HTTP.

So a web-only reviewer of a bundle-only delivery can read the delivery's **record** and
nothing else. Until this design lands, the brief for such a delivery keeps saying so.

## 2. The routes

Three GET routes under the task, read-only, no request body:

- `GET /v1/projects/{p}/tasks/{t}/delivery[?contribution=ID]` - metadata JSON:
  `{contribution: comment_id, kind, commit, base_commit, sha256, bytes, path (as
  recorded), serve_limit, links}`.
- `GET /v1/projects/{p}/tasks/{t}/delivery/bundle[?contribution=ID]` - the bundle bytes,
  `application/octet-stream`, `Content-Length` from stat, `ETag` = the recorded sha256,
  `X-Contribution: ID`.
- `GET /v1/projects/{p}/tasks/{t}/delivery/diff[?contribution=ID]` - the diff against
  the recorded base as bounded plain text (section 4). Slice 2.

Without `?contribution=` the target is the task's **current** contribution. An explicit
ID must resolve to a contribution record **on that task**; anything else is a 404
(section 6). Naming an older revision works, so a reviewer can read a superseded bundle
while reviewing the chain.

## 3. Who may call it

The same per-project live-authority check the review write path uses
(`http_service.py:9721-9722`), with the capability `reviews.write` **or**
`reviews.approve`:

- `reviews.write` is held by a project's contributors, coordinators and owners
  (`ROLE_CAPABILITIES`, `http_authority.py:369`; a viewer holds only `read`,
  `http_authority.py:370`).
- `reviews.approve` is held by coordinators and owners, and is the only review capability
  a coordinator-granted agent may hold without `reviews.write`
  (`GRANT_CAPABILITIES`, `http_authority.py:382`); a verifier who only approves still
  needs to read the code, so the route accepts either. Both capabilities are
  project-scoped and checked against live state; an expired or revoked credential fails
  the check exactly as it fails every other route.

Never public: the routes inherit the decorator's `anonymous=False` default
(`http_service.py:4100`); no form of them is ever registered anonymous.

Never another project, and the project match is checked **before any file is opened**.
The order is fixed: (1) authorize the caller for the **route's** project
(`check_authority`, `http_service.py:616`) - a member of another project is refused 403
here, before any lookup; (2) load the task by `(route project, route task)` and resolve
the contribution **within that task's own chain** - a contribution ID from another
project or another task never resolves, so it answers 404, not bytes; (3) re-check the
resolved record's own task id against the route's task id and the task's project against
the route's project, and only then (4) resolve the recorded path and open the file. The
recorded `delivery.path` string (`HOST:/absolute/...`, `docs/REVIEWS.md` delivery
example) is data the contributor wrote; the host prefix is never used to pick a host,
only the path below it is resolved, and only under a registered root (section 4).

## 4. Verification at serve time: commit, base and sha256 against the record

The route never trusts the file by name. For the bundle route the order is:

1. Authorize, resolve the record, project match (section 3).
2. Require `delivery.kind == 'bundle'`; a remote delivery answers 422 (section 6).
3. Resolve the recorded path **only** under an operator-registered delivery root
   (deployment setting `delivery_roots`, a list of absolute directory paths, read-only
   for the service; the kit's workers already drop bundles in
   `/srv/orchestra-drop/<lane>/`, so registering that root is the one-line
   operational step - `ref koopa.github.access`: deliveries reach the coordination host
   as files with their sha256 stated in the contribution). A path that escapes every
   root, or is not a regular file, or is reached through a symlink, answers 410. This is
   the `attest-delivery` discipline (`admin.py:10834` area): open without following a
   symlink, require a regular file, hash in blocks under a stated 2 GiB bound
   (`docs/REVIEWS.md:598`).
4. `git bundle list-heads <file>`, bounded to 60 seconds like attest's git
   (`docs/REVIEWS.md:598-600`). The bundle's advertised head must equal the
   contribution's recorded `commit`, full 40-hex equality.
5. Hash the file blockwise (2 GiB bound) and require the sha256 to equal the recorded
   `delivery.sha256`. Both checks pass, or nothing is served.
6. `stat` size against the serve limit (section 5), then stream in blocks with
   `Content-Length`. No lock is held; no state is written; slice 1 caches nothing
   (re-verification costs one hash of a ~63-70 MB file per request, ~0.2 s here; a cache
   keyed by `(dev, ino, size, mtime_ns)` is slice 3 if measurements say it matters).

`base_commit` is served in the metadata as the **declared** base and is checked exactly
the way attest checks it: equality with the record's own field
(`review_workflow.py:749-764`). Ancestry (that the delivered tree really sits on the
base) stays reviewer-side, as it is for attest: the reviewer who fetched the verified
bundle runs `git bundle verify`, `git fetch <bundle> <commit>`, `git diff <base>
<commit>` locally. The kit never verified ancestry for attest and does not start here.

### The diff route: a second, text form for browser reviewers

A bundle is useless in a browser pane, and the office is browser-only by policy, so the
diff route is part of the design rather than an argument for bundle-only. It is produced
**without the code host** in two local steps: fetch the just-verified bundle into the
installation's local serving repository (`git -C <serving repo> fetch <bundle path> <commit>`,
a local-path fetch, no remote URL anywhere), then `git -C <serving repo> diff
<base_commit> <commit>` and serve the text. The serving repository is operator-maintained
local state fed from delivered bundles (the release flow already fetches bundles locally;
`ref koopa.github.access`, `ref release.bundle.ci`); the service never writes it beyond
that one local fetch and never contacts any remote. Bounds of its own: the recorded base
must resolve in the serving repository (else 503, section 6); the diff text is capped by
`delivery_diff_max_bytes` (default 1 MiB) - a diff over the cap answers 413 with the
stated number and the reviewer falls back to the bundle, because a silently truncated
diff would read as a complete one; git is bounded to 60 seconds; the output is
`text/plain; charset=utf-8`.

## 5. Size limits

| Bound | Value | Pattern |
| --- | --- | --- |
| Request body (existing) | 256 KiB | `MAX_BODY_BYTES`, `http_service.py:90` |
| Attachment (existing) | 64 KiB | `MAX_ATTACHMENT_BYTES`, `http_service.py:92` |
| Static files (existing) | 4 MiB | `STATIC_MAX_BYTES`, `http_service.py:176` |
| Attest hashing (existing) | 2 GiB | `docs/REVIEWS.md:598` |
| **Bundle serve (new)** | `delivery_serve_max_bytes`, default **256 MiB** | 413 with the stated number, the `payload_too_large` shape at `http_service.py:4498` |
| **Diff text (new)** | `delivery_diff_max_bytes`, default **1 MiB** | same 413 shape |

The default ceiling sits well above every real delivery so far (~63-70 MB fixture-era
bundles) and well under attest's hashing bound; a bundle above it is refused before any
byte is streamed. `Range` requests are not supported in slice 1; a retry re-streams from
the start.

## 6. Failure table

Fixed sentences, no filesystem oracle. The three 410 rows are deliberately
indistinguishable from each other.

| Case | Status | Sentence shape |
| --- | --- | --- |
| Task/project unknown, or a record anchor | 404 | existing task-read behaviour (`_refuse_record_anchor`, `http_service.py:8146-8162`) |
| Caller not a member of THIS project with the capability | 403 | existing `check_authority` refusal (`http_service.py:616`) |
| Contribution ID unknown, forged, of another task or of another project | 404 | "Contribution not found" |
| Delivery with no bundle (`kind: remote`) | 422 | "The delivery is remote; there is no bundle to serve." |
| Bundle file missing, deleted, outside every registered root, a symlink, or not a regular file | 410 | "The delivered bundle is not available on this service." (operator action named in docs: restore the file or register the root) |
| Bundle above the serve limit / diff above the diff limit | 413 | the stated number, `payload_too_large` shape |
| Bytes on disk do not match the record: sha256 mismatch, advertised head != recorded commit, list-heads failure or timeout | 503 | "The delivered bundle does not match its recorded checksum." - the merge-slot 503 recovery pattern (`docs/HTTP_DEPLOYMENT.md`): damaged service-side state, operator recovery, nothing served |
| Diff route: recorded base does not resolve in the serving repository | 503 | same recovery pattern; the operator fetches main into the serving repo |

No new record kind, no audit entry: these are reads, and reads are not audited today
(the audit route records writes, `http_service.py:10237`). A member who can already read
the records gains bytes the records already name and checksum.

## 7. Reviewer tooling (how the Bridge and the client fetch and verify)

The contract is the metadata route plus the byte route; both tools do the same three
steps, sketched, no code:

1. Read the metadata route for the contribution; note `sha256`, `bytes`, `commit`,
   `base_commit`.
2. Stream `.../delivery/bundle` to a **temporary** file, hashing incrementally while
   streaming; compare the computed digest to the metadata `sha256` and to the `ETag`.
3. Only on match move the file into place (client: `--out` path; Bridge: its download
   folder) and hand it to git (`git bundle verify`, fetch, diff). On mismatch delete the
   temporary file and surface the fixed sentence; nothing partial is ever written to the
   destination.

- `client.py` grows `delivery TASK [--contribution ID] [--out FILE]` over the HTTP
  transport. The response is raw bytes, not the JSON envelope, so the transport needs one
  passthrough path for `application/octet-stream`. Over the SSH forced-command transport
  it answers one fixed sentence - the bytes are served over HTTP only; an on-host
  reviewer reads the recorded `delivery.path` directly.
- The Orchestra Bridge (VS Code) fetches with the session it already holds, on the
  connection whose leaf certificate it pinned at set-up
  (`docs/WEB_ONBOARDING_DESIGN.md:109`), verifies the digest as above, and offers the
  diff route's text in a read-only editor pane - the browser-only reviewer's only in-pane
  form. The web Reviews page shows the digest beside the contribution with a download
  link to the byte route.

## 8. The server never contacts the code host

The bundle route opens local files under registered roots and runs one subprocess,
`git bundle list-heads`, on a local file (the same subprocess attest-delivery runs,
`admin.py:10787`). The diff route runs `git fetch <local bundle path>` and `git diff` in
the local serving repository. No URL remote, no `ls-remote`, no clone from the network
appears anywhere in the request path; the code host is unreachable from this server by
deployment (`ref koopa.github.access`) and the design keeps it that way.

## 9. Rollback boundary and slices

An older kit simply lacks the routes: a GET answers the route-not-found 404, and nothing
else changes. The design adds no store, no record kind, no comment shape, no migration;
the two settings are read only by these routes, so an older kit ignores them. Bundles,
contribution records and attestations are untouched. Rolling back removes the routes and
returns reviewers to host-side reading; rolling forward restores them with no data step.

Proposed slices:

1. **Metadata + bundle routes** with the full check order (section 4), the two
   settings, the 413 bounds, `client.py delivery` fetch-and-verify, and the tests: the
   authorization matrix (viewer 403, foreign member 403, contributor/coordinator 200),
   remote 422, missing/deleted/unregistered/symlink 410, forged and foreign-project IDs
   404, oversize 413, digest-mismatch and head-mismatch 503 with nothing served.
2. **The diff route** over the local serving repository, with its own bound and tests
   (base-not-present 503, over-cap 413, binary files, timeout).
3. **Tooling polish**: Bridge and web page download buttons with the digest shown,
   `Range`, and the verification cache keyed by `(dev, ino, size, mtime_ns)` if
   measurements ask for it.

## 10. Open questions for review

- The default ceilings (256 MiB serve, 1 MiB diff) are proposals; the fixture-era real
  deliveries top out near 70 MB.
- Whether the diff route earns its slice 2 once slice 1 lets the client produce the same
  text locally - it exists for the browser-only reviewer who cannot.
- Route naming (`delivery` beside the task) versus a contribution-scoped form; the
  metadata route's shape is deliberately the same as the brief's contribution block so
  the page can render both from one read.
