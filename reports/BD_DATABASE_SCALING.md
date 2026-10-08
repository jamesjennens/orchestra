# Database count and write latency in Beads 1.2.2 / Dolt 2.2.0

Investigation only. Measurements use disposable synthetic databases at kit source `f615440bea48fdba534ba6efb1dfb56ed18eca2d`. This document proposes operational bounds; it does not change the database cap, dependencies, authority or production behavior.

## Finding

Each measured native write and merge-slot command sends two filtered `INFORMATION_SCHEMA.COLUMNS` checks. Their logged duration grows with the number of databases while their count stays fixed. The equivalent filtered query grows on its own; an ordinary query and native list/show remain comparatively flat. Together with the pinned source path, this supports catalog enumeration as a major source of the observed write cost. Query durations are elapsed server times, not CPU profiles or proof that every component of latency has the same cause.

## Native commands

Warning-mode medians in seconds, three samples per cell. Native timers include process startup. Counts come from `SHOW DATABASES` after excluding system schemas. At 100 databases the native supplement uses the endpoint fixture after its staged measurements; schema count is matched but issue history and table contents differ from the earlier native fixture.

| Operation | 1 DB | 5 DB | 10 DB | 20 DB | 35 DB | 50 DB | 100 DB |
|---|---:|---:|---:|---:|---:|---:|---:|
| create | 0.338 | 0.443 | 0.571 | 0.909 | 1.549 | 2.126 | 4.930 |
| update | 0.333 | 0.405 | 0.513 | 0.910 | 1.416 | 2.235 | 5.069 |
| comment | 0.320 | 0.398 | 0.565 | 0.852 | 1.341 | 2.031 | 5.069 |
| set-state | 0.576 | 0.611 | 0.788 | 1.047 | 1.557 | 2.300 | 5.420 |
| claim | 0.329 | 0.397 | 0.513 | 0.878 | 1.402 | 2.206 | 5.034 |
| merge-check | 0.239 | 0.408 | 0.486 | 0.860 | 1.238 | 2.040 | 4.848 |
| merge-acquire | 0.310 | 0.452 | 0.548 | 0.800 | 1.307 | 2.006 | 4.948 |
| merge-release | 0.317 | 0.400 | 0.606 | 0.815 | 1.335 | 2.089 | 5.084 |
| list | 0.214 | 0.216 | 0.207 | 0.209 | 0.210 | 0.233 | 0.275 |
| show | 0.225 | 0.248 | 0.195 | 0.212 | 0.223 | 0.195 | 0.264 |
| filtered-columns | 0.085 | 0.133 | 0.178 | 0.336 | 0.568 | 0.873 | 2.292 |
| ordinary-query | 0.054 | 0.052 | 0.053 | 0.058 | 0.050 | 0.055 | 0.059 |

## Endpoint commands

Warning-mode medians in seconds, three samples per cell. Timers include endpoint validation, locking and native calls, and exclude SSH and starting the measurement interpreter. New synthetic tasks keep each checkpoint/review history bounded. Prerequisite reads and fixture preparation are outside the write timer; reads performed by the endpoint itself stay inside it.

| Operation | 1 DB | 5 DB | 10 DB | 20 DB | 35 DB | 50 DB | 100 DB |
|---|---:|---:|---:|---:|---:|---:|---:|
| endpoint-create | 0.343 | 0.439 | 0.635 | 0.851 | 1.369 | 2.041 | 4.838 |
| endpoint-claim | 0.746 | 0.883 | 0.927 | 1.336 | 1.937 | 2.517 | 5.492 |
| endpoint-checkpoint | 0.506 | 0.653 | 0.758 | 1.146 | 1.682 | 2.418 | 5.149 |
| endpoint-lifecycle-scope | 0.730 | 0.885 | 1.043 | 1.376 | 1.947 | 2.479 | 5.604 |
| endpoint-lifecycle-fact | 0.721 | 0.861 | 0.909 | 1.395 | 1.894 | 2.499 | 5.502 |
| endpoint-review-contribute | 0.526 | 0.642 | 0.777 | 1.106 | 1.625 | 2.402 | 5.309 |
| endpoint-merge-check | 0.271 | 0.399 | 0.493 | 0.791 | 1.274 | 1.846 | 4.794 |
| endpoint-merge-acquire | 0.780 | 1.017 | 1.316 | 1.841 | 3.000 | 4.097 | 10.145 |
| endpoint-merge-release | 0.504 | 0.824 | 1.061 | 1.617 | 2.847 | 3.799 | 9.847 |
| endpoint-brief | 0.185 | 0.239 | 0.268 | 0.314 | 0.283 | 0.344 | 0.429 |

## Captured SQL

Debug-mode counts and catalog durations below are medians of three samples. The JSON [measurement table](data/bd-database-scaling.json) preserves ranges, load, memory and both logging modes. Catalog queries mean completed queries containing `INFORMATION_SCHEMA.COLUMNS`; the total query count counts `Starting query` events. Timings have millisecond resolution.

| Operation | SQL queries: 1 / 50 / 100 DB | Catalog queries: 1 / 50 / 100 DB | Catalog ms: 1 / 20 / 50 / 100 DB |
|---|---:|---:|---:|
| create | 64 / 64 / 64 | 2 / 2 / 2 | 58 / 520 / 1495 / 4083 |
| update | 52 / 52 / 52 | 2 / 2 / 2 | 63 / 529 / 1567 / 4278 |
| comment | 45 / 45 / 45 | 2 / 2 / 2 | 42 / 538 / 1500 / 4577 |
| set-state | 93 / 93 / 93 | 2 / 2 / 2 | 59 / 496 / 1630 / 4227 |
| claim | 48 / 48 / 48 | 2 / 2 / 2 | 59 / 526 / 1582 / 4504 |
| merge-check | 31 / 31 / 31 | 2 / 2 / 2 | 53 / 503 / 1575 / 4299 |
| merge-acquire | 43 / 43 / 43 | 2 / 2 / 2 | 57 / 571 / 1649 / 4027 |
| merge-release | 46 / 46 / 46 | 2 / 2 / 2 | 58 / 523 / 1629 / 4108 |
| list | 22 / 22 / 22 | 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| show | 33 / 33 / 33 | 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| filtered-columns | 4 / 4 / 4 | 1 / 1 / 1 | 24 / 255 / 817 / 2083 |
| ordinary-query | 4 / 4 / 4 | 0 / 0 / 0 | 0 / 0 / 0 / 0 |

The two checks are already restricted to the selected database and migration cursor tables:

```sql
SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?
AND COLUMN_NAME = 'content_hash';
```

[Beads migration source](https://github.com/gastownhall/beads/blob/6c124203e771433a3550c348771a5b5e27fd3c21/internal/storage/schema/schema.go#L860) calls `hasContentHashColumn` from migration checking and processing. Dolt 2.2.0 pins go-mysql-server revision `6d01d00bbbf3`. Its [ColumnsTable source](https://github.com/dolthub/go-mysql-server/blob/6d01d00bbbf3/sql/information_schema/columns_table.go#L145) enumerates databases, tables and columns in `AllColumns` and `columnsRowIter`. A filtered result therefore need not mean that catalog construction is confined to one schema. The runtime EXPLAIN attempt returned only a header and supplies no useful plan; source inspection and direct measurements support the explanation.

Initialization also sends catalog checks. Representative single add-project samples, including kit setup and initial backup, are in the JSON table. These single samples are not medians; elapsed initialization includes more than the catalog query durations.

| Existing DB | Add-project seconds | SQL queries | Catalog queries | Summed catalog seconds |
|---:|---:|---:|---:|---:|
| 19 | 24.521 | 1552 | 73 | 16.249 |
| 42 | 53.191 | 1553 | 73 | 43.811 |
| 48 | 62.148 | 1553 | 73 | 52.166 |
| 99 | 166.005 | 1553 | 73 | 147.739 |

## Release and backup

These are initial synthetic tracker release batches, not file deployments, upgrades or rollbacks. Each batch has 25 fresh targets with scoped integration facts. The timer covers the release operation; the 75 prerequisite fixture writes are measured separately. Release and backup entries are **single debug-mode samples**, not a repeated curve or a guarantee. Backup completion does not verify restore.

| DB | Operation | Seconds | SQL queries | Catalog queries | Summed catalog seconds |
|---:|---|---:|---:|---:|---:|
| 50 | endpoint-release25-initial | 168.933 | 7336 | 150 | 117.887 |
| 50 | backup-all | 5.764 | 150 | 0 | 0.000 |
| 100 | endpoint-release25-initial | 430.696 | 7343 | 150 | 353.138 |
| 100 | backup-all | 12.147 | 300 | 0 | 0.000 |

## Recommended bound and operator warning

Retain the default **20 project databases per server** for these pins. At 20 databases the measured native write medians remain below 1.5 seconds, but endpoint merge acquire/release already exceed that estimate because they perform additional native work. At 35, native create and set-state exceed 1.5 seconds; at 50 all measured native write/merge medians do, and at 100 they are around five seconds. Use route-specific measurements when estimating releases. This is a conservative operational recommendation for these fixtures, not a universal capacity limit.

The existing cap counts actual project databases, including archived, retired and incomplete creations; an account grant counts a different set. See [project creation limits](../docs/HTTP_DEPLOYMENT.md). Watch actual non-system database count against the configured cap, pending creations, representative write durations and backup health. Schema count alone does not capture issue volume, concurrent clients, large histories or host load.

An operator-only setup/backup status warning can report `database_count`, `project_cap`, count collection errors and whether the cap is reached, alongside the observation time. A latency warning should name the measured operation, sample interval and threshold, and distinguish unavailable measurements from fast writes. Prefer passive timing of an existing operation over creating a synthetic write in a live project. Ordinary contributors should not receive server-wide counts that expose other projects; the existing superuser view already supplies counts. Adding such a warning remains a coordinated core/UI change; this report does not implement it.

## Options and risks

| Option | Expected benefit and cost | Required validation / risk |
|---|---|---|
| Add a schema WHERE clause | The captured migration query already has one; adding the same filter offers no measured improvement. | A fix must change catalog construction or avoid repeated checks without accepting an unvalidated migration. |
| Beads/Dolt setting or newer version | May avoid enumeration or repeated migration probes; no safe bypass flag or newer-version improvement was established here. | Compare exact versions in disposable fixtures, capture SQL again, and rerun migrations, writes, authority and backup/restore tests before changing pins. |
| Move retired databases off the server | Reduces the catalog only after the databases actually leave it; hiding/archiving project records alone does not. | Operator and owner design decision: verified native plus coordination backup, restore-new rehearsal, routes/dependencies/retention handling, and explicit drop authorization. No database move or drop was performed. |
| Second Dolt server / installation | Bounds the catalog per server; comes with additional service, storage and operating cost. | Explicit routing, project authority, backup/restore ownership, cross-project dependencies and failure handling. No multi-server performance or failover was verified. |

Keep the pins and cap until an alternative has measured benefit and the above correctness evidence. [Dolt configuration documentation](https://www.dolthub.com/docs/sql-reference/server/configuration/) describes query logging; it is not evidence that a schema-scope optimization exists in these pinned binaries.

## Reproduction and limits

Use a disposable loopback runtime and fresh synthetic projects; never enable query logging against private production data for this experiment. Verify the binary hashes in the JSON table. Install the kit at the named source, prepare pinned binaries and synthetic configuration, then add projects at counts 1, 5, 10, 20, 35, 50 and 100. Count the real server catalog at every stage. Keep one measurement driver plus its server, use nice 10, and stop the stage if one-minute load exceeds 32 or available memory falls below 16 GiB.

For each ordinary route run three debug samples, restart the owned server at warning level, then run three samples again. Capture query JSON only after synthetic credential setup, slice the log around each operation, and exclude authentication/account statements and any generated credential. The [Dolt logging documentation](https://www.dolthub.com/docs/sql-reference/server/configuration/#log_level) describes debug query/latency logging. Debug and warning rounds run in that order and include a restart; differences are calibration samples, not a randomized logging-overhead estimate.

Native operations: `bd create --json`, `update --description --json`, `comments add --json`, `set-state tested=passed --reason ... --json`, `update --claim --json`, `merge-slot check/acquire/release --json`, `list --all --limit 0 --json`, and `show --json`. Direct controls use the filtered catalog query above and `SELECT COUNT(*) FROM issues`. Endpoint rounds use create, claim, fresh checkpoint CAS, lifecycle scope/fact, review contribution, merge check/acquire/release and brief. Synthetic release uses a 25-target release-deploy payload with `live_verified: false`; backup uses `backup_projects(..., all_projects=True)` and a complete/no-degraded status readback.

1234 successful timed samples are represented in the current measurement data. Initial harness failures (startup environment, invalid native slot spelling, and the post100 v1 nonexistent-marker assertion) are preserved separately and excluded. The marker assertion stopped before timed operations or fixture writes. Native list/show measurements are bounded to these synthetic project contents; they do not prove arbitrary-history read scaling. Other hosts/platforms, concurrency, alternative pins, real deployments and restore behavior remain unverified.

Measurements and verification results are separate: the contribution evidence must name the exact final document commit, its actual Linux tip/base suite results and every skip or failure. No performance improvement in production code is claimed.
