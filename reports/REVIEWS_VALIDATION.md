# Recurring workers and contribution review validation

- Windows suite: 335 tests pass, 10 platform skips. Linux suite: 335 tests pass, 7 platform skips. The final scoped briefing action adjustment also passes its focused queue/brief tests.
- Two delegated implementation modules cover resume events and structured contribution/review history. Independent review added handoff/queue regressions and identified receipt hashing/corruption and stale integration-scope defects; these were corrected.
- A disposable native flow created real Git commits and two differently named Git bundles, verified their checksums, and recorded exact delivery/base/commit references.
- Reviewer feedback overrode an older ready-for-review checkpoint. Resuming the original registered actor showed the task in its owned revision queue without changing ownership.
- A replacement actor was refused an unauthorized transfer. Current-owner handoff and explicit operator recovery both succeeded with durable intent/completion evidence.
- The replacement owner published the corrected bundle; unresolved feedback persisted until explicit response. Approval was refused while feedback remained. Approval then projected awaiting-integration without setting any lifecycle fact.
- Closed work with outstanding review/integration state remained discoverable. Historical integration evidence for another source revision could not hide a newer contribution.
- Native backup restored task review records, exact delivery pointers, resume events and handoff journals into a fresh disposable project.

Limits: readable actor identity remains trusted-team attribution, not authentication. Remote access and bundle checksums are recorded structurally; reviewers still retrieve and verify artifacts. Handoff changes assignee only, not task status or merge-slot ownership. Operator approval evidence must reflect actual project authorization. Legacy actors remain supported without automatic reassignment or registration.
