# Offline requirements slice validation

Date: 2026-09-13. Validator integration `0252a7a`; publisher integration `41d1915`. Tested as an offline prototype, with no changes to the deployed service or databases beyond ordinary coordination records.

## Independent evidence

- Full unit suite: `python -m unittest discover -s tests`. Linux: 124 tests, all pass. Windows: 124 tests, OK with five real-symlink tests skipped because the test account cannot create them. Linux exercised all five; portable mock-based link/junction checks also passed on Windows.
- Coordinator validator checks: 31 adversarial cases plus malformed CLI input, covering actual draft content, tampering, invalid identities/revisions/references and owner-approval policy.
- Coordinator publisher checks: real snapshot publication, exact receipt bytes, unchanged identical retries, conflicting-name refusal, simulated interruption before pointer replacement, recovery, old-publication preservation and tamper detection.
- Worker review corrections: reject duplicate JSON keys and malformed Unicode cleanly; normalize Markdown line endings while preserving exact manifest strings; reject links in output ancestors; verify staged bytes before installation; propagate filesystem sync failures. Regression tests reproduce these failure conditions.
- Concurrency tests exercise identical and conflicting writers. Accepted-publication behavior uses synthetic owner evidence; the kit's real requirements remain draft.

## Reproducible draft output

Publishing `docs/requirements-baseline.json` on Windows and Linux produced matching hashes:

| Content | SHA-256 |
| --- | --- |
| Content manifest identity | ef326e4d1376a2bd2d85887f69143ff785a7695693c9eae3d39e8d82f175e95f |
| Generated BRD.md bytes | 9ed8004f9db51e16ea519c0175a7bf5bfca2947f3e778b154d938393432a4364 |
| receipt.json bytes | 237b2eb239691ebf81135fad92ea3bce6a39325350b8ea66c551a4f7a8da04b6 |

The original manually formatted BRD is retained. Its source identity is the same; its file bytes are not expected to match the new layout.

## Delegation and limits

Hermes/DeepSeek implemented each task in an isolated source copy. The coordinator verified each plan acknowledgement before sending a separate execution instruction, reviewed the code and ran independent checks before local integration. The publisher worker hit its turn limit before reporting; resuming the same session preserved its honest checkpoint, including an untested final patch. The coordinator tested and corrected the final implementation.

No GitHub PR, public release, production deployment, accepted requirements baseline, native export adapter or automatic impact propagation is claimed. Filesystem recovery covers process interruption on local filesystems, not power loss or hostile concurrent filesystem mutation. The offline acceptance object checks declared owner policy and exact content binding; it does not authenticate the named people.
