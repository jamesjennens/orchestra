"""Project-owned file manifests for datasets and task results.

The helper deliberately stores references to files rather than copying their
contents.  It is standard-library-only and keeps the access policy with the
project that owns the artifact root.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


class ArtifactError(ValueError):
    """Raised when an artifact path or manifest entry is invalid."""


_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIELDS = ("source", "dataset", "as_of", "fields", "securities", "actor", "task", "metadata")


def _safe_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ArtifactError(f"{name} must be a safe path component")
    if value in {".", ".."}:
        raise ArtifactError(f"{name} cannot be traversal")
    return value


def _relative_path(value: str | os.PathLike[str]) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ArtifactError("path must be text")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ArtifactError("path must be nonempty text")
    if os.path.isabs(raw) or PurePosixPath(raw.replace("\\", "/")).is_absolute():
        raise ArtifactError("artifact path must be relative")
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError("artifact path contains an unsafe component")
    for part in parts:
        _safe_component(part, "path component")
    return "/".join(parts)


def _json_value(value: Any, name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ArtifactError(f"{name} must be JSON-compatible") from exc
    return value


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Hold a cross-platform advisory lock on a sibling lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


class ArtifactStore:
    """Register and verify files beneath a project-owned artifact root."""

    def __init__(self, root: str | os.PathLike[str], manifest: str = "manifest.jsonl"):
        root_path = Path(root)
        if root_path.exists() and root_path.is_symlink():
            raise ArtifactError("artifact root cannot be a symlink")
        self.root = root_path.resolve()
        if not manifest or Path(manifest).name != manifest:
            raise ArtifactError("manifest must be a file name")
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / manifest
        self.lock_path = self.manifest_path.with_name(f".{manifest}.lock")

    def dataset_path(self, dataset: str, filename: str) -> Path:
        return self.path_for("datasets", dataset, filename)

    def result_path(self, task: str, filename: str) -> Path:
        return self.path_for("results", task, filename)

    def path_for(self, *parts: str) -> Path:
        if len(parts) < 2:
            raise ArtifactError("an artifact location needs a namespace and name")
        relative = _relative_path("/".join(parts))
        path = self.root / Path(relative)
        self._check_within_root(path, must_exist=False)
        return path

    def register(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str,
        source: str | None = None,
        dataset: str | None = None,
        as_of: str | None = None,
        fields: list[str] | None = None,
        securities: list[str] | None = None,
        actor: str | None = None,
        task: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        relative = _relative_path(path)
        _safe_component(kind, "kind")
        file_path = self.root / Path(relative)
        self._check_within_root(file_path, must_exist=True)
        if not file_path.is_file():
            raise ArtifactError("artifact path must be a regular file")
        checksum, size = _sha256(file_path)
        record = {
            "schema_version": 1,
            "path": relative,
            "kind": kind,
            "sha256": checksum,
            "bytes": size,
            "source": source,
            "dataset": dataset,
            "as_of": as_of,
            "fields": fields,
            "securities": securities,
            "actor": actor,
            "task": task,
            "metadata": metadata or {},
        }
        for key in _FIELDS:
            _json_value(record[key], key)
        with _exclusive_lock(self.lock_path):
            rows = self._read_unlocked()
            matches = [row for row in rows if row["path"] == relative]
            if matches:
                if len(matches) != 1:
                    raise ArtifactError(f"manifest has duplicate path: {relative}")
                if matches[0] != record:
                    if matches[0]["sha256"] != checksum:
                        raise ArtifactError(f"artifact bytes changed for registered path: {relative}")
                    raise ArtifactError(f"conflicting duplicate registration: {relative}")
                return {"created": False, "record": dict(record)}
            self._write_unlocked(rows + [record])
        return {"created": True, "record": dict(record)}

    def entries(self, *, verify: bool = True) -> list[dict[str, Any]]:
        with _exclusive_lock(self.lock_path):
            rows = self._read_unlocked()
            if verify:
                for row in rows:
                    self._verify_row(row)
            return [dict(row) for row in rows]

    def verify(self, path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
        rows = self.entries(verify=False)
        selected = rows if path is None else [row for row in rows if row["path"] == _relative_path(path)]
        if path is not None and not selected:
            raise ArtifactError("artifact is not registered")
        for row in selected:
            self._verify_row(row)
        return [dict(row) for row in selected]

    def _check_within_root(self, path: Path, *, must_exist: bool) -> None:
        if must_exist and not path.exists():
            raise ArtifactError("artifact file does not exist")
        if path.exists() and path.is_symlink():
            raise ArtifactError("symlink artifact paths are not allowed")
        resolved = path.resolve(strict=must_exist)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactError("artifact path escapes the artifact root") from exc

    def _verify_row(self, row: dict[str, Any]) -> None:
        if not isinstance(row, dict) or row.get("schema_version") != 1:
            raise ArtifactError("invalid manifest entry")
        relative = _relative_path(row.get("path", ""))
        path = self.root / Path(relative)
        self._check_within_root(path, must_exist=True)
        checksum, size = _sha256(path)
        if checksum != row.get("sha256") or size != row.get("bytes"):
            raise ArtifactError(f"checksum or size mismatch for registered path: {relative}")

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.manifest_path.exists():
            return []
        if self.manifest_path.is_symlink():
            raise ArtifactError("manifest cannot be a symlink")
        rows: list[dict[str, Any]] = []
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ArtifactError("manifest entries must be objects")
                        rows.append(value)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("cannot read artifact manifest") from exc
        return rows

    def _write_unlocked(self, rows: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=self.root, prefix=".manifest-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        except OSError as exc:
            raise ArtifactError("cannot atomically update artifact manifest") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
