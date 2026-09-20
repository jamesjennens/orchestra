"""Version and source provenance for installed Orchestra kit components."""
import json
import hashlib
from pathlib import Path
import re


KIT_VERSION = "0.1.0"
SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _manifest(root):
    try:
        manifest = json.loads((Path(root) / "provenance.json").read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or
                manifest.get("component") != "orchestra-kit" or
                manifest.get("version") != _read_version(root) or
                not isinstance(manifest.get("source_commit"), str) or
                (manifest["source_commit"] != "unknown" and
                 not SOURCE_COMMIT.fullmatch(manifest["source_commit"])) or
                not isinstance(manifest.get("build_id"), str) or not manifest["build_id"] or
                not isinstance(manifest.get("files"), dict) or not manifest["files"]):
            return None
        for name, digest in manifest["files"].items():
            if (not isinstance(name, str) or Path(name).name != name or
                    name == "provenance.json" or not isinstance(digest, str) or
                    not re.fullmatch(r"[0-9a-f]{64}", digest)):
                return None
            path = Path(root) / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return None
        return manifest
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _read_version(root):
    try:
        value = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return KIT_VERSION
    return value or KIT_VERSION


def _source_commit(root):
    manifest = _manifest(root)
    return manifest["source_commit"] if manifest else "unknown"

def _build_id(root):
    manifest = _manifest(root)
    return manifest["build_id"] if manifest else "unknown"


def report(root=None, component="kit"):
    """Return immutable display metadata without requiring a Git checkout."""
    root = Path(root or Path(__file__).resolve().parent)
    return {
        "component": component,
        "version": _read_version(root),
        "source_commit": _source_commit(root),
        "build_id": _build_id(root),
        "path": str(root),
    }


def line(metadata):
    return "Orchestra %s: version %s, source %s" % (
        metadata["component"], metadata["version"], metadata["source_commit"],
    )
