# Session registration validation

- Windows: 296 tests pass, 10 platform skips. Linux: 296 tests pass, 7 platform skips.
- Unit coverage includes repeated readable names, collision rejection against native actors and registry records, retry reconciliation, changed-name retry rejection, invalid/corrupt records, symlink rejection, actor requirements, bootstrap actor propagation and registry backup/restore.
- Six concurrent requests on a disposable native deployment returned four actors: three identical request IDs reconciled to one actor, and three independent requests with the same readable name got distinct actors.
- A changed name with the same request ID was refused.
- Empty-directory `worker.py start` printed its actor and matching onboarding; repeating the request returned the same registration.
- Native backup included the registry. Restoration into a fresh disposable project preserved the registration exactly and `session show` retrieved it.

Scope: allocation is serialized per project, with UUID-based actors checked against recorded native actor fields and registry entries. This is not authentication or an exclusivity lease. Legacy actors still work; deliberately sharing an actor ID remains possible in the trusted-team model. Restore clones must not become parallel live authorities.
