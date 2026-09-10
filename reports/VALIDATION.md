# Pilot validation — 2026-09-10

Passed on a disposable Linux x86-64 deployment reached from Windows using Python/OpenSSH, with Beads 1.2.2 and Dolt 2.2.0.

- Five local unit tests passed: UTF-8/file transport, invalid host rejection, no mutation retry after SSH failure, rendered correction backlinks/current filtering, and issue-path traversal rejection.
- Two independent SSH client processes read the same saved plan.
- Three simultaneous claim races each produced exactly one winner.
- Concurrent comments both persisted; later corrections linked back to originals in generated Markdown.
- A second project did not return the first project's records.
- Closed task state survived a service restart.
- Native backup restored closed state and all three comments into a new database. Writes in that restored database did not affect the original.

Full integration run: 87.72 seconds. See integration.json for recorded results and pinned artifact hashes.

The user service is configured for boot/logout persistence (linger enabled); a full machine reboot was not performed. Host-loss/off-machine recovery, large-team load, untrusted-user isolation and unattended backup scheduling were not tested. This is a small-team pilot, not a production availability guarantee.
