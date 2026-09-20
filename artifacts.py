"""Project-owned file manifests for datasets and task results.

The helper deliberately stores references to files rather than copying their
contents.  It is standard-library-only and keeps the access policy with the
project that owns the artifact root.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


class ArtifactError(ValueError):
    """Raised when an artifact path or manifest entry is invalid."""


_WINDOWS_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECORD_FIELDS = {
    "schema_version", "path", "kind", "sha256", "bytes", "source", "dataset",
    "as_of", "fields", "securities", "actor", "task", "metadata",
}
_TEXT_FIELDS = {"source", "dataset", "as_of", "actor", "task"}
_LIST_FIELDS = {"fields", "securities"}
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.01


def _safe_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ArtifactError(f"{name} must be a safe path component")
    if value in {".", ".."}:
        raise ArtifactError(f"{name} cannot be traversal")
    if value.endswith(".") or _WINDOWS_DEVICE.match(value):
        raise ArtifactError(f"{name} cannot be a Windows path alias")
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
    if any(part in {"", ".."} for part in parts):
        raise ArtifactError("artifact path contains an unsafe component")
    parts = [part for part in parts if part != "."]
    if not parts:
        raise ArtifactError("artifact path contains no file")
    for part in parts:
        _safe_component(part, "path component")
    return "/".join(parts)


def _json_value(value: Any, name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ArtifactError(f"{name} must be JSON-compatible") from exc
    return value


def _sidecar_path(path: Path) -> None:
    if path.is_symlink():
        raise ArtifactError(f"refusing symlink sidecar: {path.name}")


def _windows_lock(handle, path: Path, adapter=None, timeout=_LOCK_TIMEOUT, poll=_LOCK_POLL) -> None:
    if adapter is None:
        import msvcrt as adapter
    deadline = time.monotonic() + timeout
    while True:
        try:
            adapter.locking(handle.fileno(), adapter.LK_NBLCK, 1)
            return
        except OSError as exc:
            contention = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or (
                getattr(exc, "winerror", None) in {33, 36}
            )
            if not contention:
                raise ArtifactError(f"cannot lock {path.name}: {exc}") from exc
            if time.monotonic() >= deadline:
                raise ArtifactError(f"timed out acquiring {path.name} after {timeout:.1f}s") from exc
            time.sleep(poll)


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Hold a bounded, symlink-safe cross-platform advisory lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _sidecar_path(path)
    with path.open("a+b") as handle:
        _sidecar_path(path)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            handle.seek(0)
            _windows_lock(handle, path)
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
        if root_path.is_symlink():
            raise ArtifactError("artifact root cannot be a symlink")
        self.root = root_path.resolve()
        if not manifest or Path(manifest).name != manifest:
            raise ArtifactError("manifest must be a file name")
        _safe_component(manifest, "manifest")
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / manifest
        self.lock_path = self.manifest_path.with_name(f".{manifest}.lock")
        self._reserved = {
            manifest.replace("\\", "/").casefold() if os.name == "nt" else manifest.replace("\\", "/"),
            self.lock_path.name.casefold() if os.name == "nt" else self.lock_path.name,
        }
        _sidecar_path(self.manifest_path)
        _sidecar_path(self.lock_path)

    def dataset_path(self, dataset: str, filename: str) -> Path:
        return self.path_for("datasets", dataset, filename)

    def result_path(self, task: str, filename: str) -> Path:
        return self.path_for("results", task, filename)

    def path_for(self, *parts: str) -> Path:
        if len(parts) < 2:
            raise ArtifactError("an artifact location needs a namespace and name")
        relative = _relative_path("/".join(parts))
        self._reject_reserved(relative)
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
        self._reject_reserved(relative)
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
        self._validate_record(record)
        with _exclusive_lock(self.lock_path):
            rows = self._read_unlocked()
            matches = [row for row in rows if self._path_key(row["path"]) == self._path_key(relative)]
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
        selected = rows if path is None else [row for row in rows if self._path_key(row["path"]) == self._path_key(_relative_path(path))]
        if path is not None and not selected:
            raise ArtifactError("artifact is not registered")
        for row in selected:
            self._verify_row(row)
        return [dict(row) for row in selected]

    @staticmethod
    def _path_key(relative: str) -> str:
        return relative.casefold() if os.name == "nt" else relative

    def _reject_reserved(self, relative: str) -> None:
        candidate = self._path_key(relative)
        if candidate in self._reserved:
            raise ArtifactError(f"artifact path is reserved: {relative}")

    def _check_within_root(self, path: Path, *, must_exist: bool) -> None:
        if must_exist and not path.exists():
            raise ArtifactError("artifact file does not exist")
        if path.is_symlink():
            raise ArtifactError("symlink artifact paths are not allowed")
        resolved = path.resolve(strict=must_exist)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactError("artifact path escapes the artifact root") from exc

    def _verify_row(self, row: dict[str, Any]) -> None:
        relative = self._validate_record(row)
        path = self.root / Path(relative)
        self._check_within_root(path, must_exist=True)
        checksum, size = _sha256(path)
        if checksum != row.get("sha256") or size != row.get("bytes"):
            raise ArtifactError(f"checksum or size mismatch for registered path: {relative}")

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if self.manifest_path.is_symlink():
            raise ArtifactError("manifest cannot be a symlink")
        if not self.manifest_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        value = json.loads(line)
                        self._validate_record(value, rows)
                        rows.append(value)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("cannot read artifact manifest") from exc
        return rows

    def _validate_record(self, row: Any, rows: list[dict[str, Any]] | None = None) -> str:
        if not isinstance(row, dict):
            raise ArtifactError("manifest entry must be an object")
        if set(row) != _RECORD_FIELDS:
            raise ArtifactError("manifest entry has invalid fields")
        if type(row["schema_version"]) is not int or row["schema_version"] != 1:
            raise ArtifactError("manifest schema_version must be integer 1")
        relative = _relative_path(row["path"])
        if row["path"] != relative:
            raise ArtifactError("manifest path must be canonical")
        self._reject_reserved(relative)
        if not isinstance(row["kind"], str) or not _COMPONENT.fullmatch(row["kind"]):
            raise ArtifactError("manifest kind is unsafe")
        if not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ArtifactError(f"invalid checksum for {relative}")
        if type(row["bytes"]) is not int or row["bytes"] < 0:
            raise ArtifactError(f"invalid byte count for {relative}")
        for key in _TEXT_FIELDS:
            if row[key] is not None and not isinstance(row[key], str):
                raise ArtifactError(f"manifest {key} must be text or null")
        for key in _LIST_FIELDS:
            if row[key] is not None and (not isinstance(row[key], list) or any(not isinstance(item, str) for item in row[key])):
                raise ArtifactError(f"manifest {key} must be a string list or null")
        _json_value(row["metadata"], "metadata")
        if rows is not None and any(self._path_key(existing["path"]) == self._path_key(relative) for existing in rows):
            raise ArtifactError(f"manifest has duplicate path: {relative}")
        return relative

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
