# Installation and recovery

## Install on Linux

Use an ordinary service account with a writable home, Python 3.10+, user systemd, and HTTPS access to GitHub release assets. Copy this kit to `/home/beads/beads-team-kit` in this example. Each deployment needs its own root, port and service name. Installation refuses unmanaged binaries or an existing service with the same name.

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime install --port 13317 --unit beads-team.service
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime add-project example
```

Project names use 2–24 lowercase letters/digits, starting with a letter. Run these commands as the service account. An administrator should enable its user manager at boot and after logout:

```sh
sudo loginctl enable-linger beads
loginctl show-user beads -p Linger
systemctl --user is-active beads-team.service
```

The database listens on loopback only. Its generated credential is stored in the runtime's `deployment.private.json` (0600); runtime permissions are 0700. Contributors do not need the SQL password. Do not publish this directory or open the database port on the network. The installer configures metrics off for this deployment.

If initial installation fails, it stops the service and preserves files for inspection. Do not rerun by deleting the runtime: investigate the service journal first. Repeating installation of an already initialized deployment checks its pins, port and connectivity.

## Connect a contributor

Install Python 3.10+ and OpenSSH on their machine. Configure an SSH alias `beads-team` for the server/service account with their own key; verify the server host key on first connection. Confirm an ordinary `ssh beads-team` works before using the noninteractive client.

Copy `client.example.json` to `client.local.json`, adjust the host and paths, then run:

```sh
python client.py --config client.local.json --project example --actor alex/session1 -- ready --json
python client.py --config client.local.json --project example --actor alex/session1 -- refresh
python client.py --config client.local.json --project example --actor alex/session1 -- view
```

Keep local config outside committed project content or ignored. The client transports arguments and UTF-8 file contents as JSON over SSH. It does not copy source code or run builds. `--body-file`, `--design-file`, `--file` and `-f` read files on the contributor's machine. The endpoint exposes the routine issue commands; setup and maintenance use admin.py on the host.

Copy templates/READ_ME_FIRST.md and docs/WORKFLOW.md into each project repository, fill in project details, and add links to README and AGENTS.md. This makes the entry instructions discoverable by later agents without needing a pasted chat message.

## Backups

Run after meaningful work and before maintenance:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup example
```

This invokes Beads' native Dolt backup into `runtime/backups/example`. Same-host backups protect against some mistakes, not loss of the server. Arrange an ordinary scheduled, encrypted off-machine copy of completed backups using your existing backup system. The kit does not install a backup timer. Preserve the kit/version pins and a protected copy of deployment configuration separately; JSONL views are useful exports but are not a substitute for the native backup.

Restore drills deliberately create a new project:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime restore-new example examplerestore
```

It refuses a populated destination, restores status and comments, and retains original issue IDs. Inspect records and comments before any cutover. Do not run both copies as live coordination trackers. A complete host-loss recovery requires reinstalling the pinned kit on a replacement host, placing the saved backup under its backups directory, using restore-new, verifying it, then updating client project/host settings. That cross-host procedure has not yet been tested; the automated drill covers restore into a new database on the same server.

## Maintenance

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime service status
journalctl --user -u beads-team.service -n 50
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime service restart
```

If a client times out, inspect the task before retrying a mutation: the write may have committed. Do not replace a lost response with an assumed failure.

To retire the service, back up first, then `systemctl --user disable --now beads-team.service`. Preserve the runtime until recovery is no longer needed. Remove only the exact unit installed for that deployment and run `systemctl --user daemon-reload`. Linger is an account-wide setting: leave it enabled if other user services need it.

## Repeat validation

Local checks: `python -m unittest discover -s tests -v`.

Create two disposable projects and run the integration suite with an unused restore destination:

```sh
python tests/integration.py --config client.local.json --project testone --other-project testtwo --restore-project testrestore --output reports/local-integration.json
```

This restarts the configured server and creates synthetic records. Run it only on a disposable deployment, never a live team server. It checks two independent SSH clients, concurrent claims/comments, project separation, rendered corrections, restart and backup restoration.
