# Operational tranche validation — 2026-09-15

The operational tranche was developed against synthetic records on isolated deployments. No live project database was used for these integration tests. Beads 1.2.2 and Dolt 2.2.0 remain pinned.

| Check | Result |
| --- | --- |
| Windows unit suite | 257 run, OK; eight platform/symlink privilege skips |
| Linux unit suite | 257 run, OK; seven Windows-only skips |
| Four concurrent child creations | Distinct native IDs; same request reconciles; changed content refused |
| Two merge-slot contenders | One holder; nonholder release refused; exact-context resumption succeeds |
| Native lifecycle | Initial facts, corrections and source-scope rollover round-trip through export/current views |
| Native decisions | Creation validation and lint pass with Decision, Rationale, Alternatives Considered |
| Existing SSH/recovery suite | Cross-client UTF-8, claim races, comments/corrections, project separation, restart and restore pass |
| Separate deployment recovery | Records, comments, labels, lifecycle descriptions, pending request journal and merge context preserved |
| Real Linux local transport | No nested SSH; POSIX wrapper/refresh work from another directory; actor and literal arguments preserved |

Machine-readable reports: [operations](operations-integration.json), [restart/restore](recovery-integration.json), [deployment transfer](deployment-recovery.json), [local transport](local-transport.json).

Two DeepSeek workers supplied activity and transport changes in isolated copies with explicit actors and acknowledged plans. Independent reviews fixed inherited CMD ERRORLEVEL/delayed expansion behavior, included comments on event records in activity, and prevented failed output flush from advancing a cursor. Coordinator tests fixed native correction parsing: initial events use `Set DIM to VALUE`, while corrections use `Changed DIM from OLD to NEW`.

Recovery review identified that native database backups omit the new coordination journals. Backups now preserve a validated sidecar, mark unfinished snapshots pending, and lock the native/sidecar pair during backup and restore. Fault-injection tests cover lost responses, partial updates, conflicting request IDs, incomplete backup markers and restore path validation. These are not promises of a cross-filesystem transaction or exactly-once external execution.

The transfer drill used two independent services/credentials on the same Linux host. It did not simulate losing that host. The office's actual Copilot permissions and contributor-account arrangement were not exercised. Legacy project migration and existing backup schedules remain separate operator work.

One integration attempt ran alongside a different refresh and observed different CURRENT/COORDINATION snapshots; the final operational run was isolated and passed. Generated files are individually atomic projections, not a multi-file read transaction. The native database remains authoritative.
