# Requirements integration validation

2026-09-13, source commit `e0eae0a`.

- Windows: 161 unit tests, OK with five symlink-privilege skips. Linux: all 161 pass in an isolated source directory; no live service restart or schema change.
- Actual kit data: verified current titles/descriptions against the retained draft; appended 13 explicit revision comments; one subsequent saved native export reconstructed the original manifest hash `ef326e4d1376a2bd2d85887f69143ff785a7695693c9eae3d39e8d82f175e95f`. Published that exact draft locally. Original bodies, comments and baseline wording remain intact.
- Synthetic end-to-end test: native export -> validated snapshot -> BRD publication -> revised requirement -> dependent design/task reassessment -> explicit reaffirmation. Old BRD bytes and historical evidence are preserved. Missing links, cycles, revision regression, known historical rewrites, accepted-change evidence and successive-baseline history are tested.
- Independent read-only review found and reproduced two history-resolution gaps and one false-positive reassessment case. Each was corrected and the reviewer verified the focused tests and final reproduction. Current reaffirmation cannot resurrect an already-resolved historical change.
- Hermes/DeepSeek implemented the gate in an isolated copy. Its initial plan registration stalled; the coordinator recorded its bounded plan with explicit attribution before authorizing code. Worker checkpoint: `01a09b9e-a6a4-7c39-a756-5c3b3c22596a`. Coordinator added full durable plan text, exact canonical context/ID matching, destination binding and atomic receipts with regressions. Independent review found no substantive launch-before-ack bypass within the stated trusted-team boundary.
- Disposable canonical task `kittrial-pth.21.1`: real SSH gate refused a launch without a receipt; registered and read back the plan; verified the claim; ran a harmless local Python marker; refused a changed plan without creating the second marker. Plan comment `01a09ba4-4840-77ed-98a2-73ce07897f77`. Synthetic task closed after its report.

These are CLI capabilities, not a scheduler or OS sandbox. Impact views do not mutate task state; owners still provide exact acceptance decisions and work-graph links. The requirement baseline remains draft. No public repository publication or live runtime deployment occurred.
