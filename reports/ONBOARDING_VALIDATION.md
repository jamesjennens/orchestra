# Server onboarding validation

Validated with pinned Beads/Dolt and Python on Windows and Linux:

- 283 unit tests pass: 9 platform skips on Windows, 7 on Linux.
- New tests cover composed onboarding, missing project instructions, fixed catalog/path rejection, symlink rejection, UTF-8 limits, client routing and exact project text backup/restore.
- Disposable native deployment returns shared/project instructions using the server bootstrap from `/tmp`, with no repository checkout or local kit dependency.
- Native backup sidecar contains the exact private project entry point after installation.
- Shared documents remain installed kit assets. Project instructions are owner-maintained private runtime state; generated views do not overwrite them.

Scope: onboarding and document retrieval are read-only. Client setup, repository access, build dependencies and task authorization remain explicit project instructions. The bootstrap does not provide credentials, clone repositories automatically, initialize a second database or grant code integration authority. New backup sidecars containing ONBOARDING.md require this kit version or newer for restore.
