# Database count and write latency: Beads 1.2.2 and 1.3.1 on Dolt 2.2.0

Investigation only. Measurements use disposable synthetic databases at kit source `f615440bea48fdba534ba6efb1dfb56ed18eca2d`. This document proposes operational bounds; it does not change the database cap, dependencies, authority or production behavior.

Keep the Beads 1.2.2 and Dolt 2.2.0 pins: the available evidence does not establish a cost reduction from Beads 1.3.1, and the kit is not compatible with it yet. Migrating a database to the new schema prevents Beads 1.2.2 from using it, including reads; this is not a reversible binary-only upgrade.

## Finding

Each measured pinned-version native write and merge-slot command sends two filtered `INFORMATION_SCHEMA.COLUMNS` checks. Their logged duration grows with the number of databases while their count stays fixed. The equivalent filtered query grows on its own; an ordinary query and native list/show remain comparatively flat. Together with the pinned source path, this supports catalog enumeration as a major source of the observed write cost. Query durations are elapsed server times, not CPU profiles or proof that every component of latency has the same cause.

Beads 1.3.1 replaces the two cursor-column probes, but each measured write gains one lease-column check (new in 1.3.1) and eleven `INFORMATION_SCHEMA.TABLES` probes; reads gain one TABLES probe. The COLUMNS-only count below omits that work. Our target-only migrated fixture favours 1.3.1: the other databases retain 26 tables/222 columns, while a migrated database has 30 tables/252 columns. The independent second review measured create at 30 fully migrated databases as 1.627 s, versus 1.364 s with 1.2.2, with non-overlapping observed ranges. On this evidence 1.3.1 does not reduce the cost. Our limited 50-database create/claim decreases remain observations of the biased fixture; add-project was about 1.6 times slower.

## Native commands

The pinned fixture has 26 tables and 222 columns per project database. The task records 17,944 column rows at 52 real databases (about 345 per database), so real projects may impose greater catalog cost. These timings do not establish a per-database cost for arbitrary real installations.

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

The excess create cost per additional database is about 26 ms from 1 to 10 databases, 36 ms from 1 to 35, and 46 ms from 1 to 100, calculated after subtracting the one-database median. The high-count growth is faster than a constant linear increment; the 100-database fixture has different issue history, so the data do not isolate a universal nonlinear exponent.

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

Debug-mode counts and catalog durations below are medians of three samples, except filtered-columns at 50 databases: that cell pools the six debug samples in native-curve-v2 and native-post-v1 (817, 858, 785, 820, 908, 792 ms), whose median is 818.5 ms. The JSON [measurement table](data/bd-database-scaling.json) links the unchanged raw samples containing ranges, load, memory and both logging modes; derived aggregates can be regenerated with `python3 reports/data/summarize_bd_scaling.py`. In all tables, catalog queries and catalog ms count only completed `INFORMATION_SCHEMA.COLUMNS` queries; they omit `INFORMATION_SCHEMA.TABLES` and other catalog access. A lower count here therefore does not imply less total catalog work; the total query count counts `Starting query` events. Timings have millisecond resolution.

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
| filtered-columns | 4 / 4 / 4 | 1 / 1 / 1 | 24 / 255 / 818.5 / 2083 |
| ordinary-query | 4 / 4 / 4 | 0 / 0 / 0 | 0 / 0 / 0 / 0 |

The two checks are already restricted to the selected database and migration cursor tables:

```sql
SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?
AND COLUMN_NAME = 'content_hash';
```

[Beads migration source](https://github.com/gastownhall/beads/blob/6c124203e771433a3550c348771a5b5e27fd3c21/internal/storage/schema/schema.go#L860) calls `hasContentHashColumn` from migration checking and processing. Dolt 2.2.0 pins go-mysql-server revision `6d01d00bbbf3`. Its [ColumnsTable source](https://github.com/dolthub/go-mysql-server/blob/6d01d00bbbf3/sql/information_schema/columns_table.go#L145) enumerates databases, tables and columns in `AllColumns` and `columnsRowIter`. A filtered result therefore need not mean that catalog construction is confined to one schema. The runtime EXPLAIN attempt returned only a header and supplies no useful plan; source inspection and direct measurements support the explanation.

Initialization also sends catalog checks. Representative single add-project samples, including kit setup and initial backup, are in the JSON table. These single samples are not medians; elapsed initialization includes more than the catalog query durations.

| Fixture | Existing DB | Add-project seconds | SQL queries | Catalog queries | Summed catalog seconds |
|---|---:|---:|---:|---:|---:|
| native | 19 | 24.521 | 1552 | 73 | 16.249 |
| native | 42 | 53.191 | 1553 | 73 | 43.811 |
| native | 48 | 62.148 | 1553 | 73 | 52.166 |
| endpoint | 99 | 166.005 | 1553 | 73 | 147.739 |

## Release and backup

These are initial synthetic tracker release batches, not file deployments, upgrades or rollbacks. Each batch has 25 fresh targets with scoped integration facts. The timer covers the release operation; the 75 prerequisite fixture writes are measured separately. Release and backup entries are **single debug-mode samples**, not a repeated curve or a guarantee. These two observations at 50 and 100 databases are not a release or backup curve: there is no repeated release/backup sample at 1 or 20 databases. Backup completion does not verify restore. `backup --all` issued no catalog queries (0 of 150 at 50 projects, 0 of 300 at 100); its observed growth follows the number of projects backed up, rather than the catalog-probe mechanism measured on writes.

| DB | Operation | Seconds | SQL queries | Catalog queries | Summed catalog seconds |
|---:|---|---:|---:|---:|---:|
| 50 | endpoint-release25-initial | 168.933 | 7336 | 150 | 117.887 |
| 50 | backup-all | 5.764 | 150 | 0 | 0.000 |
| 100 | endpoint-release25-initial | 430.696 | 7343 | 150 | 353.138 |
| 100 | backup-all | 12.147 | 300 | 0 | 0.000 |

## Recommended bound and operator warning

Retain the default **20 project names per server** for the pinned versions. The native create median increases from 0.338 s at one database to 0.909 s at 20, about 2.7 times; at 50 it is 2.126 s, and at 100 it is 4.930 s. This measured slowdown is the basis for the conservative recommendation, rather than an unsourced 1.5 s service target. Endpoint merge acquire/release cost more because they do additional native work. Use route-specific measurements for release estimates; these synthetic fixtures do not establish a universal capacity limit.

The kit already reports `project_databases: {used, limit}` from `admin.project_setup_status`, and `admin.py project-creations --usage` exposes that count to operators. `project_creation.server_names` counts names on disk under `projects/`, retired directories and creations holding names, including damaged creation records. **It does not query the server catalog.** Unrepresented databases can be missed and reservations can precede an actual database. The default cap stops only creation from the web; operator `add-project` is not stopped. The independent review observed used 30 with limit 20 and an operator creation still permitted. See [project creation limits](../docs/HTTP_DEPLOYMENT.md).

Propose an operator warning at 10 counted names for a default limit of 20, and an explicit reached/exceeded warning at the configured limit. Ten is an early planning threshold: the measured create median is already about 1.7 times the one-database value, while leaving room to arrange backups or a second server. This warning is proposed, not implemented, and should distinguish the filesystem count from an operator-authorized actual non-system catalog count. Show observation time, count errors and incomplete creations; never label an unavailable count as zero. Representative passive write timings and backup health complement either count. Contributor views must retain existing server-wide information boundaries. No live synthetic writes or core/UI behavior changes are part of this report.

## Options and risks

| Option | Expected benefit and cost | Required validation / risk |
|---|---|---|
| Add a schema WHERE clause | The captured migration query already has one; adding the same filter offers no measured improvement. | A fix must change catalog construction or avoid repeated checks without accepting an unvalidated migration. |
| Beads 1.3.0 / 1.3.1 | The [released probe change](https://github.com/gastownhall/beads/commit/3594a3762) uses `SHOW COLUMNS FROM <cursor table> LIKE 'content_hash'`, avoiding the two per-write cursor-table probes on `INFORMATION_SCHEMA.COLUMNS` (one other COLUMNS probe per write remains). The replacement alone does not reduce the observed cost: additional TABLES probes, migration expansion and initialization work offset it. Keep the current pin; see the comparison and compatibility failures below. | Coordinate all clients, verify official and static builds, explicitly consent to shared-server migrations, and test authority, schema, backups and restore before changing any pin. |
| Newer Dolt | A server-side catalog optimization could reduce remaining probes, but no newer Dolt binary was measured here. | Compare an exact candidate against the same catalog and SQL before claiming improvement; do not infer it from a newer release number. |
| Move retired databases off the server | Reduces the catalog only after the databases actually leave it; hiding/archiving project records alone does not. | Operator and owner design decision: verified native plus coordination backup, restore-new rehearsal, routes/dependencies/retention handling, and explicit drop authorization. No database move or drop was performed. |
| Second Dolt server / installation | Bounds the catalog per server; comes with additional service, storage and operating cost. | Explicit routing, project authority, backup/restore ownership, cross-project dependencies and failure handling. No multi-server performance or failover was verified. |

Keep the pins and cap until an alternative has measured benefit and the above correctness evidence. [Dolt configuration documentation](https://www.dolthub.com/docs/sql-reference/server/configuration/) describes query logging; it is not evidence that a schema-scope optimization exists in these pinned binaries.

## Released Beads option and upgrade requirements

The probe replacement is Beads commit `3594a3762` ([schema source at v1.3.1](https://github.com/gastownhall/beads/blob/v1.3.1/internal/storage/schema/schema.go#L1192)); it is present in v1.3.0 and v1.3.1, and absent from the pinned v1.2.2. `SHOW COLUMNS` targets one cursor table; the code compares the returned field exactly because the underscore in a LIKE pattern is a wildcard. Removing those probes does not establish lower write latency: the newer binary adds other catalog probes, and the available full-fleet evidence is slower.

[Beads 1.3.0 upgrade notes](https://github.com/gastownhall/beads/releases/tag/v1.3.0#upgrading-notes) describe a v53-to-v66 main-schema change and a separate clone-local migration series. Shared servers require deliberate migration consent: upgrade every client, take backups using the old binary first, then run `bd migrate schema` once; scripted standing consent is `BD_ALLOW_REMOTE_MIGRATE=1`. Remote-backed stores require a designated migrator and sync handling as documented there. [1.3.1 notes](https://github.com/gastownhall/beads/releases/tag/v1.3.1#upgrading-notes) add no migration beyond 1.3.0, but upgrading from 1.2.2 still includes that change. Consent is confined to new disposable comparison copies in this investigation, not a recommendation to bypass a live gate.

A kit pin change also requires a reproducible 1.3.1 static build for the hosts that cannot run upstream's dynamically linked Linux asset; the existing `tools/build_bd_static.sh` and `versions.json` static receipt are specific to 1.2.2. Verify hashes, ABI/architecture, fresh init, all-client migration/compatibility, endpoint/authority behavior, native plus coordination backups, and restore/rollback before an operator changes pins. After migration from schema v53 to v66, Beads 1.2.2 refuses every command on that database, reads included, with a schema-version mismatch; the independent reviewer confirmed it still writes an unmigrated sibling. Upstream provides no data downgrade. Before consent/migration, the reviewer found 1.3.1 writes refused and `bd list` failed with `table not found: leases`, despite the refusal claiming reads keep working. The kit is not ready for this upgrade: the independent reviewer ran 87 real-bd tests and found 22 failures on 1.3.1 versus zero on 1.2.2, including a held claim returning 503 uncertain instead of 409, changed refusal text and flags absent from the kit tables. These are independent review findings, not additional worker test results. No binary replacement or migration occurred in any live runtime.

Replacing the cursor probes does not make initialization constant-time. Of the 73 catalog checks captured during pinned initialization, 57 are other statements. New migrations and other catalog checks can still dominate; use the measured new-version SQL counts rather than extrapolating all 73 away.

As of this source review, [Dolt 2.4.2 release notes](https://github.com/dolthub/dolt/releases/tag/v2.4.2) do not establish a fix for this workload. That tag is commit `9c835f4dc7974bb50083ab121c097cc903a91997`; its [go.mod](https://github.com/dolthub/dolt/blob/9c835f4dc7974bb50083ab121c097cc903a91997/go/go.mod) pins go-mysql-server `77662b650e46`. The corresponding [ColumnsTable source](https://github.com/dolthub/go-mysql-server/blob/77662b650e46/sql/information_schema/columns_table.go#L145) still enumerates databases in `AllColumns` and `columnsRowIter`. This is source evidence about those methods, not proof that every query plan or runtime cost is unchanged. A newer Dolt performance improvement is unverified here; the comparison keeps Dolt 2.2.0 fixed.

### Matched version comparison

Official bd 1.3.1 archive SHA256 `3219443a9734b89b93fb16ee8d65844759fa1b3cd3cf139c606b7353cfb0715c`; extracted binary SHA256 `a8f48d771b9e11eccfced4aed72923b59eda746f7a9b425e7ca1af7a51251653`. Fresh stopped synthetic seed copies are used at 1, 20 and 50 databases. Each repetition begins at the stated count, including add-project; it ends one database higher. The target schema is migrated explicitly only in the new-version copy. Debug and warning timings are separate.

Completed comparison: 126 successful timed samples, zero failures, including 18 explicit migration samples. Three samples per cell; warning-mode medians and ranges are in the linked raw data.

| Existing DB | Version | create s | claim s | add-project s |
|---:|---|---:|---:|---:|
| 1 | 1.2.2 | 0.364 | 0.315 | 6.821 |
| 1 | 1.3.1 | 0.450 | 0.353 | 10.811 |
| 20 | 1.2.2 | 1.064 | 0.865 | 26.370 |
| 20 | 1.3.1 | 1.075 | 0.906 | 38.733 |
| 50 | 1.2.2 | 2.227 | 2.009 | 61.902 |
| 50 | 1.3.1 | 2.077 | 1.903 | 100.774 |

Ratios below are newer/pinned warning-mode medians; below 1 is less elapsed time. Three samples are descriptive measurements, not a statistical significance or live upgrade claim.

| DB | create ratio | claim ratio | add-project ratio |
|---:|---:|---:|---:|
| 1 | 1.23 | 1.12 | 1.59 |
| 20 | 1.01 | 1.05 | 1.47 |
| 50 | 0.93 | 0.95 | 1.63 |

Explicit `bd migrate schema` on the new-version target is outside the create/claim/add-project timers. Each row has three fresh repetitions. These costs still grow with the catalog population.

| Existing DB | warning median s | warning range s | debug median s |
|---:|---:|---:|---:|
| 1 | 3.121 | 3.102–3.234 | 3.105 |
| 20 | 15.409 | 15.219–16.212 | 15.617 |
| 50 | 42.557 | 42.209–42.699 | 41.678 |

Fleet planning: multiplying the observed warning-mode per-target median by the database count gives about 308 s (5.1 minutes) for 20 databases and 2,128 s (35.5 minutes) for 50. These are estimates, not measured fleet migrations: our fixture migrated only one target at a time, and catalog expansion during a whole-fleet migration can change the cost. The independent reviewer actually migrated 30 databases in about 12 minutes.

Debug SQL medians (three captures each). Each row reports create / claim / add-project, at the stated existing database count.

| DB | Version | All query counts | Catalog query counts | SHOW COLUMNS counts |
|---:|---|---:|---:|---:|
| 1 | 1.2.2 | 64 / 48 / 1552 | 2 / 2 / 73 | 0 / 0 / 0 |
| 1 | 1.3.1 | 99 / 77 / 2343 | 1 / 1 / 107 | 2 / 2 / 16 |
| 20 | 1.2.2 | 64 / 48 / 1552 | 2 / 2 / 73 | 0 / 0 / 0 |
| 20 | 1.3.1 | 99 / 77 / 2343 | 1 / 1 / 107 | 2 / 2 / 16 |
| 50 | 1.2.2 | 64 / 48 / 1553 | 2 / 2 / 73 | 0 / 0 / 0 |
| 50 | 1.3.1 | 99 / 77 / 2343 | 1 / 1 / 107 | 2 / 2 / 16 |

The two cursor-table probes are replaced, but new-version create/claim still issue one COLUMNS query checking `leases.granted_node` and eleven TABLES queries. In our captured `1.3.1-50-1-create.json`, TABLES queries sum to 756 ms, while the COLUMNS query takes 773 ms; the table above counts only the latter. The independent review observed about 50 ms per TABLES probe at 30 databases; together they cost more than the remaining COLUMNS check. Reads also acquire a TABLES probe. This work grows with database count, so halving the displayed COLUMNS count is not a halving of elapsed work. The evidence does not support a cost-reducing upgrade.

Initialization remains expensive. The 57 other pinned initialization catalog statements remain relevant, and 1.3.1 adds schema/migration work; the new-version counts above must not be inferred by subtracting all pinned probes. Migration is timed separately from steady writes and add-project. The copies begin with identical pinned schema/data; each new-version target is explicitly migrated, while the other catalog databases retain the pinned schema. Only our target is migrated; a fully migrated fleet has 30 tables/252 columns per database rather than 26/222 and therefore more catalog work. The independent fully migrated 30-database comparison was slower on 1.3.1, as stated above.

Each fresh repetition uses copies of a stopped synthetic 26-table/222-column seed database, with its provisioned synthetic merge slot. Only the target project needs a workspace directory; other cloned databases contribute the same seed catalog schema. This controls catalog population but differs from the original sequentially initialized fixture and from real project histories. Each create/claim pair uses one fresh task; add-project starts at exactly 1/20/50 existing databases, then ends one higher. It includes kit initialization, merge-slot provisioning and its initial backup. Debug and warning runs use separate fresh copies and are ordered, not randomized. Load/memory and all timings remain in the raw samples.

The first harness attempt failed before sending SQL because a copied `.beads/dolt-server.port` still named the seed port despite updated metadata. It was excluded and retained in private investigation evidence; the corrected fresh comparison updates both port sources. No product source was changed, no existing fixture was modified, and every owned comparison server stopped.

[Raw comparison samples](data/raw/bd-version-comparison.json) and [measurement metadata](data/bd-database-scaling.json) preserve all 126 timings and hashes. The six committed raw files are the measurements; the large redundant derived summary has been removed. Raw-row trace names identify captures kept in private review evidence (an archive with SHA256 `d93c7ae10c27fa1793bebc62b31096d8a7aaa2f241d95eea0aaaf820655ce9a0`), not files promised in this repository. The deterministic summarizer groups by actual raw filename, operation, database count, logging and version; it invents no post50 group names. Independent second-review findings are attributed to review `01a11b9a-76a2-732a-ad4f-25a0fed1b292` on the task; its evidence is kept privately and was not changed or copied.

## Reproduction and limits

Use a disposable loopback runtime and fresh synthetic projects; never enable query logging against private production data for this experiment. Verify the binary hashes in the JSON table. Install the kit at the named source, prepare pinned binaries and synthetic configuration, then add projects at counts 1, 5, 10, 20, 35, 50 and 100. Count the real server catalog at every stage. Keep one measurement driver plus its server, use nice 10, and stop the stage if one-minute load exceeds 32 or available memory falls below 16 GiB.

For each ordinary route run three debug samples, restart the owned server at warning level, then run three samples again. Capture query JSON only after synthetic credential setup, slice the log around each operation, and exclude authentication/account statements and any generated credential. The [Dolt logging documentation](https://www.dolthub.com/docs/sql-reference/server/configuration/#log_level) describes debug query/latency logging. Debug and warning rounds run in that order and include a restart; differences are calibration samples, not a randomized logging-overhead estimate.

Native operations: `bd create --json`, `update --description --json`, `comments add --json`, `set-state tested=passed --reason ... --json`, `update --claim --json`, `merge-slot check/acquire/release --json`, `list --all --limit 0 --json`, and `show --json`. Direct controls use the filtered catalog query above and `SELECT COUNT(*) FROM issues`. Endpoint rounds use create, claim, fresh checkpoint CAS, lifecycle scope/fact, review contribution, merge check/acquire/release and brief. Synthetic release uses a 25-target release-deploy payload with `live_verified: false`; backup uses `backup_projects(..., all_projects=True)` and a complete/no-degraded status readback.

The historical measurement data represent 1,234 successful timed samples; the matched version comparison adds 126, for 1,360 accepted timings in total. Initial harness failures (startup environment, invalid native slot spelling, and the post100 v1 nonexistent-marker assertion) are preserved separately and excluded. The marker assertion stopped before timed operations or fixture writes. Native list/show measurements are bounded to these synthetic project contents; they do not prove arbitrary-history read scaling. Other hosts/platforms, concurrency, newer Dolt, real deployments and restore behavior remain unverified. The bd comparison above has its own scope and does not verify an operational upgrade.

Measurements and verification results are separate: the contribution evidence must name the exact final document commit, its actual Linux tip/base suite results and every skip or failure. No performance improvement in production code is claimed.
