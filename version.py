"""Version and source provenance for installed Orchestra kit components."""
import json
from pathlib import Path


KIT_VERSION = "0.1.0"


def _read_version(root):
    try:
        value = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return KIT_VERSION
    return value or KIT_VERSION


def _source_commit(root):
    try:
        manifest = json.loads((Path(root) / "provenance.json").read_text(encoding="utf-8"))
        if (isinstance(manifest, dict) and manifest.get("schema_version") == 1 and
                manifest.get("component") == "orchestra-kit" and
                manifest.get("version") == _read_version(root) and
                isinstance(manifest.get("source_commit"), str) and
                isinstance(manifest.get("build_id"), str) and manifest["build_id"]):
            return manifest["source_commit"]
    except (OSError, UnicodeError, ValueError, TypeError):
        pass
    return "unknown"

def _build_id(root):
    try:
        manifest = json.loads((Path(root) / "provenance.json").read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("schema_version") == 1 and manifest.get("component") == "orchestra-kit":
            value = manifest.get("build_id")
            return value if isinstance(value, str) and value else "unknown"
    except (OSError, UnicodeError, ValueError, TypeError):
        pass
    return "unknown"


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
