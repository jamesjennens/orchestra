# Project-owned artifacts and manifests

`artifacts.py` provides a small, standard-library-only convention for files that
should be shared by a project's workers without copying their contents into the
source repository. Create an `ArtifactStore` over a project-owned artifact root:

```python
from artifacts import ArtifactStore

store = ArtifactStore("/srv/project-artifacts")
dataset = store.dataset_path("market-sample", "prices.csv")
result = store.result_path("kittrial-5bb.14", "long-test.json")
```

The caller creates and writes the file, then registers the relative path:

```python
store.register(
    "datasets/market-sample/prices.csv",
    kind="dataset",
    source="vendor-export",
    dataset="market-sample",
    as_of="2026-09-18",
    fields=["date", "close"],
    securities=["SYNTHETIC"],
    actor="worker/session",
    task="kittrial-5bb.14",
)
store.register(
    "results/kittrial-5bb.14/long-test.json",
    kind="test-output",
    task="kittrial-5bb.14",
    metadata={"suite": "synthetic-long-test"},
)
```

The manifest is a JSONL file in the artifact root. Each entry records the
relative path, byte count, SHA-256, source/dataset/as-of context, optional
fields and securities, actor/task attribution, and generic JSON metadata.
`register` is idempotent for an exact entry, rejects changed bytes and
conflicting duplicate metadata, and serializes concurrent writers. `entries()`
verifies every registered file by default; `verify(path)` verifies a selected
entry.

Paths are deliberately portable and safe: absolute paths, traversal,
symlinked files and root escapes are rejected. The helper does not grant
access. Project owners must choose filesystem permissions, encryption,
retention/deletion schedules, backup policy and any required data-governance
controls. Keep restricted datasets and test output in the project-owned
artifact location, not in Git, and publish only synthetic or explicitly
approved summaries.
