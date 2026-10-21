# Files attached to a task: design note (kittrial-5bb.350)

**Status:** design only. No code changes with this note. The coordinator and James decide; slices are filed only after that. Base: main `ab93a73`.

## The problem in one paragraph

A coordinator sometimes needs to hand a worker files the worker cannot reach (an input spreadsheet, a log, a drawing), and the worker sometimes needs to hand results back. Today Orchestra cannot carry them:

- the web task-create route validates attachments and then refuses them with `501 not_implemented` (`http_service.py` `validate_attachments`, line 288);
- proposals carry only `{name, sha256}` references (`proposal_records.py` 275-285);
- the CLI's `attachments` are UTF-8 text inlined into one request of at most 2 MB (`client.py` 135-170).

So files travel by git, a share or e-mail, outside the task's record. This note proposes how Orchestra itself could store such files, show them on the task and let only the right people fetch them. It must not weaken anything the kit already guarantees.

## What this note decides

There are five questions from the task, plus the rollback boundary and how to build it. Each has options and one recommendation. **Recommendations are in bold.** Where a real installation's constraint shapes a choice, it is named as an example. The design is for any installation.

Throughout, "a task file" means one stored file attached to one task, with a direction:

- **to the worker:** put there by a coordinator, for the assignee;
- **from the worker:** put there by the assignee, for the coordinators.

---

## 1. Who may attach, and who may download

### 1a. Who may attach

| Option | Who | For | Against |
|---|---|---|---|
| A | The project's coordinators (people with the coordinator or owner role, and agents holding the coordinator grant) for "to the worker"; the task's current assignee for "from the worker" | Matches the request exactly. Both sides have a clear reason to write. | A reviewer cannot attach evidence. |
| B | Any contributor on the project | Simple. | Anyone could fill any task with files. The task's file list stops meaning "what the coordinator gave me". |
| C | A, plus the reviewer named in an open review request | Lets a reviewer hand back a reproduction. | A third writer, and a third kind of file, in version 1. |

**Recommendation: A.**

- "Coordinators" is exactly the set `http_auth.coordinators_of` already computes: members whose role holds `coordinate`, and granted agents.
- "The assignee" is the task's `assignee` field, compared exactly with the acting actor, as the lifecycle rules already do (`lifecycle.py` 199-220).
- A reviewer who needs to share something comments with a reference today, and C can be a later slice.

### 1b. Who may download

| Option | Who | For | Against |
|---|---|---|---|
| A | The current assignee and the project's coordinators | "Only those who may act on the task". The smallest set. | A reviewer or another worker must ask. |
| B | Everyone who can read the task (every project member, viewers included) | Like comments. | A file handed to one worker becomes readable by the whole project, so a coordinator could not share a customer file with just the assignee. |
| C | A, plus the reviewer named in an open review request, for files marked "from the worker" | A reviewer can check the result the worker attached. | One more rule to test. |

**Recommendation: A in version 1, C as the first follow-up.** Fixed points in every option:

- **Never another project.** The project is part of the route, and a file is bound to its project and task when it is stored.
- **Agents only through their own credential.** An agent's credential works only on projects it was granted, as every route already enforces. It may fetch a task's files only when it is that task's assignee, or holds the coordinator grant in that project.
- **A former assignee loses access** the moment the task is reassigned. The rule is read live on every request; nothing is copied into a share list.
- Superusers and installation operators keep their usual reach (they can read the disk anyway). Their downloads are audited like anyone's (section 4).

---

## 2. Where files live, limits, integrity, and what happens over time

### 2a. Where the bytes live

| Option | Where | For | Against |
|---|---|---|---|
| A | A kit-owned folder beside the project, `projects/<name>/.task-files/`, one file per stored blob, named by its sha256 | No tracker change. Same disk as the project. Content-addressed: the same file attached twice is stored once. Easy to back up as files. | A new folder to back up and prune. |
| B | Inside the tracker (base64 in a comment or a field) | One place for everything. | It bloats every export. The read cache (`read_cache.py`, one `bd export --all` of 58-66 MB on a large project) would carry every file to every reader. Dolt history would keep every version forever. |
| C | An external object store (S3 and the like) | Scales. | Needs outbound network from the server. Some installations' servers cannot reach the internet or even the code host. Another credential to manage. |

**Recommendation: A.**

- The folder sits next to the kit's other project-owned state (`.read-cache`, `.history-snapshots`, `.assignee-followons`), is refused if it is a symlink, and is never inside the tracker or source control.
- Stored names are server-made (`<first two hex>/<sha256>`). The person's file name is display metadata only, as `docs/HTTP_TRANSPORT_DESIGN.md` section 7 already requires.

### 2b. The record of a file

The bytes need a record: who attached what, to which task, when, and with which digest.

| Option | Record | For | Against |
|---|---|---|---|
| A | A reserved comment on the task, `Kind: task-file-v1 {...}`, holding `{name, sha256, bytes, media_type, direction, note}`; the author is the comment's author | The sha256 is in the task's own history for good, as the task asks. `history`, `brief` and the "changes since your checkpoint" section see it. The native backup carries it. An older kit reads it as an ordinary comment (section 6). | A comment per file. |
| B | A new host journal under the project (`.task-files-journal/<sha>.json`, like `.assignee-followons`) | Keeps comments clean. | The coordination backup's restore list is fixed: an OLDER kit refuses to restore a backup holding a journal it does not know (`admin.py` `validate_coordination_files`, which raises "Invalid coordination backup path"; see docs/OPERATIONS.md on `.assignee-followons/`). A rollback would then break restores. |
| C | Only the files folder, with a manifest file | Simple. | No record in the task's history, and nothing an older kit could show. |

**Recommendation: A.**

- The record is the comment. The folder holds bytes only, plus a small per-project index rebuilt from the comments when needed.
- **Withdrawing a file** is a second reserved comment, `Kind: task-file-withdrawn-v1 {sha256, reason}`, so nothing is erased from history.
- The bytes of a withdrawn file are removed by an operator's purge (2e), never by a click on the web page.

### 2c. Limits

The figures are recommended defaults. Each is one setting in `deployment.private.json`, read by the service and the endpoint alike.

| Limit | Recommended default | Why this size |
|---|---|---|
| One file | 25 MiB | Spreadsheets, PDFs and logs fit. It must also pass the reverse proxy in front of the site, whose own body limit (nginx `client_max_body_size`, for example) the operator raises to match; that is said in the install note. |
| Files on one task | 20 | Enough for a hand-off. Larger sets belong in an archive file or a share. |
| Bytes on one task | 100 MiB | |
| Bytes in one project | 2 GiB | Disk growth stays visible and bounded (section 4, backups). |
| Free disk kept | Refuse an upload that would leave less than 1 GiB free on the project's disk | A full disk must never take the tracker down with it. |
| Types | Any bytes. Stored and served as opaque `application/octet-stream`; the declared type is display metadata only. | Refusing types is easy to get around (rename a file) and stops real work (a `.zip`, a `.xlsx`). What makes untrusted content safe is how it is served (section 4), not its extension. |

A reached limit is one sentence naming the limit and the setting, as the kit's other refusals do. "Uploads stop at 25 MiB a file (task_files.max_file_bytes); this one is 31 MiB" is an example.

### 2d. Integrity

- **On upload, the server computes the sha256 while it streams the body to a temporary file.** It keeps the file (rename into place) only when the byte count matches `Content-Length`. When the uploader also sent `X-Orchestra-SHA256`, the digest must match too. The record (2b) carries that digest.
- **On download, the server sends the digest it recorded** in `X-Orchestra-SHA256` (and `Content-Length`). Every kit client verifies it before keeping the file:
  - the Bridge;
  - the endpoint client;
  - the web page, which hashes with SubtleCrypto before saving.

  Each writes to a temporary name, hashes, and renames only on a match. On a mismatch the file is deleted and one sentence says the download did not match its record.
- **A periodic check:** an operator command (`admin.py task-files verify PROJECT`) re-hashes every stored file against its record. That finds a disk that rotted, or a file swapped by hand.
- The server does not re-hash on every download. Doing so would read a 25 MiB file twice for every fetch, and the client check already catches a bad copy.

### 2e. Close, reassign, retention

| Event | Recommended rule |
|---|---|
| Task reassigned | The new assignee may fetch at once; the old one may not (1b is read live). Files are untouched. |
| Task closed | Files stay. Coordinators may still fetch them, and the closed task's last assignee may not (it no longer acts on the task). |
| Task reopened | As before the close: the assignee of the reopened task may fetch again. |
| Retention | Files of a task closed more than 180 days ago may be purged by an operator (`admin.py task-files purge PROJECT --closed-before DATE`). Nothing is deleted automatically: deleting evidence is an operator's decision, written in the purge's own audit line. |
| Withdrawn file | Not listed, not downloadable. Its bytes are removed at the next purge. If another task still refers to the same sha256 (content-addressed storage), the bytes stay until no live record refers to them. |
| Project archived | Read-only: files are still downloadable by coordinators, and no new file can be attached. (The kit has no project-removal command; if an operator removes a project's folder by hand, its `.task-files` go with it.) |

---

## 3. The routes

### 3a. HTTP (the web service), used by the web page, the Bridge and curl

All under the task, so the project and task are always part of the authorization:

| Route | What it does |
|---|---|
| `GET /v1/projects/{pid}/tasks/{tid}/files` | The task's files: `{sha256, name, bytes, media_type, direction, by, at, note}`, newest first, bounded with a count. It is read fresh, not from the read cache (section 4). |
| `PUT /v1/projects/{pid}/tasks/{tid}/files?name=NAME&direction=to-worker\|from-worker` | Upload one file. The body is the raw bytes (`Content-Type: application/octet-stream`). `Content-Length` is required; `X-Orchestra-SHA256` is optional and checked when present. It answers `201` with the record, or `200` when the same file with the same name is already attached (an idempotent retry). |
| `GET /v1/projects/{pid}/tasks/{tid}/files/{sha256}` | Download: the bytes, served as in section 4, with `X-Orchestra-SHA256`. |
| `POST /v1/projects/{pid}/tasks/{tid}/files/{sha256}/withdraw` | Withdraw (writes the withdrawal record). Allowed for the file's own author and for coordinators. |

**Why raw bytes and not JSON with base64:**

- Today's API reads every body whole into memory, after checking it is under 256 KiB (`http_service.py` `_read_body`, `MAX_BODY_BYTES`). Base64 would add a third.
- Ten concurrent 25 MiB uploads read whole would hold about 330 MiB in the web service's memory. A service on a small office server must not depend on that.
- The upload route is the ONE route that streams its body to disk in 64 KiB steps. Every other route keeps today's JSON reading.

The existing task-create `attachments` field, which today answers 501, would then bind to this store. Version 1 keeps the 501 and points to the files route instead, so no existing client changes behaviour. (docs/HTTP_DEPLOYMENT.md, about line 3014, says uploads there are "validated and bounded but only their metadata and digest are retained". The code refuses them with 501, so slice 1 corrects that sentence.)

### 3b. The endpoint and client, for lanes over SSH

The SSH transport carries one JSON request of at most 2 MB (`client.py` `MAX_WIRE`; `endpoint.py` reads stdin up to 2 MB). A file needs its own framing:

| Option | How | For | Against |
|---|---|---|---|
| A | Chunks: the client sends the file as numbered base64 pieces of at most 1 MB, each an ordinary request, and the endpoint assembles them | No new framing. | Many round trips. A half-assembled file must be cleaned up. Base64 overhead. |
| B | A framed stream on the same SSH command: one JSON header line `{action: "file-put", project, task, name, direction, bytes, sha256}`, then exactly `bytes` raw bytes. The endpoint streams them to the temporary file with the same caps and digest check. `file-get` answers with a JSON header line, then the raw bytes. | One round trip. Constant memory. The same streaming code as the HTTP route. | A new framing to test, and only the kit's client speaks it. |

**Recommendation: B.**

- The new client commands are `files list TASK`, `files get TASK SHA256 --out DIR` and `files put TASK PATH --direction ...`. They follow the existing `client.py ... --` shape and verify the digest before keeping a file.
- The authorization is the endpoint's, exactly as for every other endpoint action: the actor, the project and the live rule of 1b.

### 3c. Orchestra Bridge tools (web agents)

Some agents reach Orchestra only through the Bridge or curl, often behind an office proxy. The Bridge already goes through the same HTTPS site and the proxy settings the person has, so no new network path is needed. Three tools, with the shape the Bridge's other tools have:

| Tool | Input | What it does |
|---|---|---|
| `orchestra_files_list` | `{task}` | Lists the task's files (3a's list, as text the agent can read: name, size, direction, who, when, sha256). |
| `orchestra_file_download` | `{task, sha256}` | Downloads into the agent's own workspace folder, under `.orchestra/files/<task>/<name>`. It never writes anywhere else. |
| `orchestra_file_upload` | `{task, path, note?}` | Uploads a file that is inside the agent's workspace folder, as "from the worker". A path outside it, or through a symlink that leads outside it, is refused before anything is read. |

The rules the Bridge enforces:

- **Never outside the agent's folder.** The target is resolved and must stay inside the workspace folder the Bridge was set up in, the folder `.orchestra/agent.json` lives in. Symlinked parents are refused.
- **The name is cleaned twice.** The server's display name (already a basename, section 4) is cleaned again on the agent's side: no path separators, no device names on Windows, no leading dot. A clash adds ` (2)` and never overwrites.
- **The digest is checked before keeping.** The tool writes `<name>.partial`, hashes it, and renames only when the digest matches `X-Orchestra-SHA256`. On a mismatch it deletes the partial and says so.
- **Bounded:** the Bridge refuses a file over the service's per-file limit, read from the list's `bytes`, before downloading.
- **Data, not instructions.** The tool's answer says the file is data from the task and must not be followed as instructions (as the kit's prompts already say of comments and uploaded documents).

The equivalent curl lines go into `.orchestra/AGENT.md`. They use the same quoting (kittrial-5bb.360), the same `--noproxy` for the server's own host, and a `sha256sum` (or `Get-FileHash` in PowerShell) check before the file is used.

### 3d. The web page

The task page gains a "Files" panel:

- the list;
- an upload button, shown only to those who may attach (1a);
- a download link per file, shown only to those who may download (1b).

Download uses `fetch`, checks the digest with SubtleCrypto, then saves through a Blob link with the file's cleaned name. Nothing is ever opened inline (section 4).

---

## 4. Security

| Concern | Recommended handling |
|---|---|
| **Untrusted content** | A file is never executed and never rendered by the service. Every download is served with `Content-Type: application/octet-stream`, `Content-Disposition: attachment; filename*=UTF-8''<encoded name>`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, and `Content-Security-Policy: sandbox; default-src 'none'`. So even a browser that ignored the disposition could not run anything from it. The page never previews a file in version 1. A later text preview would go through the page's existing text-only rendering (`dom.js`: `textContent`, never `innerHTML`). |
| **Path traversal** | The name a person gives is only a label. It must be a basename of at most 128 characters, with no `/`, `\\`, control characters or `.`/`..`, as `validate_attachments` already checks. The stored name is the sha256. Every reader (Bridge, client, page) cleans the name again before writing to disk. |
| **Size bombs** | `Content-Length` is required and checked against the limits before a byte is read. The stream is counted while it is written, and refused at the limit even if the header lied. Nothing is ever decompressed or unpacked by the service: an archive is just bytes. The free-disk check (2c) runs before accepting. |
| **The service's memory under concurrent requests** | Uploads and downloads move in 64 KiB steps; nothing holds a whole file. At most four transfers run at once in the web service (a setting); a fifth gets `503` with `Retry-After`, as a busy service should. One actor has at most two at once. The service has no per-actor rate limit today, so these two caps are the bound. |
| **Audit of who fetched what** | Every upload, download, withdrawal and purge writes one line to the service's existing audit (`http_auth.audit`: time, request id, user, actor, credential, project, action, outcome), with the task and sha256 in the reason. The project's admins read it with `GET /v1/projects/{pid}/audit`. Uploads and withdrawals are also in the task's history (2b). Downloads are not, because they are reads, and every read would otherwise make every checkpoint stale. |
| **Backups** | See 4a. |
| **The read cache** | The file LIST is read from the records (comments, which the export carries) and checked against the folder on every list request. It is not taken from the read cache's file list, because there is none: the cache holds only the export and per-task histories, keyed on the tracker's HEAD commit. An upload writes its record comment, which commits, so the next export read sees it. The bytes are never cached. |
| **Secrets** | As with task text (docs/BRIEFINGS.md and docs/WORKER_GUIDE.md since kittrial-5bb.362): a file is visible to everyone who may download it, and its record stays in history. A leaked secret is rotated, not withdrawn. |

### 4a. Backups

| Option | What | For | Against |
|---|---|---|---|
| A | Back the files up as files: `backups/<name>.task-files/`, a copy of the folder plus a manifest of `{sha256, bytes}`, verified on restore | Files survive a disk loss with the project. No change to the coordination sidecar. | Backups grow by the files' size. |
| B | Leave them out of backups and say so | Backups stay small. | A restored project has records for files it no longer has. |
| C | Put them in the coordination sidecar (base64, as feedback quarantines are) | One file. | The sidecar is one JSON document: 2 GiB of base64 in it is not workable. An older kit's restore would refuse the unknown path (section 6). |

**Recommendation: A.**

- With a separate folder, the coordination sidecar is untouched, and an older kit's restore of that sidecar still works.
- The backup report states the files' total size, so growth is seen. A project over its limit is refused new uploads, so the backup's growth is bounded too.
- A restore without the folder (an older kit, or a copy that lost it) still restores the project. The file records then say "the stored file is missing" on download, never a wrong file.

---

## 5. The office case

- **No outbound network from the server is needed.** Files go from the person's browser or the agent's Bridge to the Orchestra site, and from the site to the disk beside the project. A server that cannot reach the code host, or the internet, works the same.
- **The existing HTTPS site is the only path.** That is the same certificate, proxy bypass and `--noproxy` rules the set-up wizard already gives (kittrial-5bb.348 and .360).
- **One operator step:** raise the reverse proxy's body limit, if there is one in front, to the per-file limit. The install note would say so, and a new host check would make a test upload of the limit's size through the public address, to show whether the proxy cuts it.
- **Workers over SSH** move files over the same SSH connection their client already uses (3b).

---

## 6. The rollback boundary

What an OLDER kit (one without this feature) does when an installation rolls back after files were stored:

| Thing | Older kit |
|---|---|
| The `Kind: task-file-v1` and `task-file-withdrawn-v1` comments | Read as ordinary comments: shown in history and brief as their JSON text (name, size, digest, direction), counted as activity. Nothing is refused, and nothing is lost. |
| The `.task-files/` folder | Not read, not changed, not deleted. A later upgrade finds it as it was. |
| The routes | Absent: the page has no Files panel, the Bridge's file tools answer that the service does not offer them, curl gets `404`. |
| Backups made by the newer kit | The native backup and the coordination sidecar are exactly as before. An older kit restores them; the separate `.task-files` backup folder is ignored, so the restored project has the records and no bytes. |
| Backups made by the older kit while rolled back | They leave out `.task-files`. Files attached before the rollback are then in no newer backup until the kit is upgraded again. The operator note says to back up with the newer kit, or copy the folder by hand, before a rollback. |
| The coordination sidecar | Untouched by this design, on purpose (2b option B, 4a option C). So an older kit's fixed restore list never meets a path it does not know. That is the failure the `.assignee-followons` journal causes today (docs/OPERATIONS.md). |

So a rollback loses no record and breaks no restore. It only hides the files until the upgrade, and that is said in the release note.

---

## 7. How it could be built (proposed slices, filed only after the decision)

1. **Storage, records and the HTTP routes:**
   - the `.task-files` folder;
   - the streaming upload with its caps, disk check and digest;
   - the list, download and withdraw routes;
   - the audit lines.

   Tests use real streams larger than the caps and concurrent uploads at the transfer limit.
2. **The web panel:** list, upload, download with the digest check; nothing inline.
3. **The Bridge tools** (in the Bridge's own repository) and the AGENT.md curl lines.
4. **The SSH client** `files` commands and the endpoint's framed stream.
5. **Operations:**
   - backups of the folder and their restore check;
   - `admin.py task-files verify` and `purge`;
   - the proxy upload check;
   - the install and release notes.
6. **Follow-up:** the reviewer's read access (1b option C); binding the task-create `attachments` field to this store.

## 8. What James and the coordinator are asked to decide

1. Who may attach and download: **1a A, 1b A, with C later.**
2. Where files live and how they are recorded: **2a A, 2b A.**
3. The default limits in 2c (25 MiB a file, 20 files and 100 MiB a task, 2 GiB a project, 1 GiB disk kept free), or other figures.
4. Retention: **an operator purge of tasks closed for more than 180 days, nothing automatic**, or another period.
5. The SSH framing: **3b B.**
6. Backups: **4a A, the files backed up as files beside the project's backup.**

Nothing else in the kit changes until these are agreed.
