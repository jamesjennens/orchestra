# Briefing validation

Validated 2026-09-15 against pinned Beads 1.2.2 and Dolt 2.2.0.

- Windows: 274 unit tests, 8 platform skips, all passing.
- Linux: 274 unit tests, 7 platform skips, all passing.
- Independent review added 17 regressions and found an unresolved-item replacement loophole. Carried items must now remain unchanged; changing their meaning requires explicit supersession with evidence.
- A disposable native deployment exercised explicit author/time provenance, stale activity rejection, unresolved-item carry-forward rejection, explicit resolution, separate unknown lifecycle facts and unchanged raw `show` access.
- Six native history pages reconstructed a large Unicode comment exactly. A comment added during pagination remained outside the original snapshot and caused the current checkpoint's freshness flag to change.

These checks validate transport and record semantics, not the truth of checkpoint prose. Existing tasks require deliberate reconciliation before they acquire a structured current checkpoint. Operator corruption recovery and history-cache retention remain documented manual operations. Unit tests cover snapshot expiry, timestamp ties, timezone filters, changed activity and concurrent checkpoint branches.
