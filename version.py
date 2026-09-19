"""Version and source provenance for installed Orchestra kit components."""
import os
import subprocess
from pathlib import Path


KIT_VERSION = "0.1.0"


def _read_version(root):
    try:
        value = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return KIT_VERSION
    return value or KIT_VERSION


def _source_commit(root):
    configured = os.environ.get("ORCHESTRA_SOURCE_COMMIT", "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    value = result.stdout.strip()
    return value or "unknown"


def report(root=None, component="kit"):
    """Return immutable display metadata without requiring a Git checkout."""
    root = Path(root or Path(__file__).resolve().parent)
    return {
        "component": component,
        "version": _read_version(root),
        "source_commit": _source_commit(root),
        "path": str(root),
    }


def line(metadata):
    return "Orchestra %s: version %s, source %s" % (
        metadata["component"], metadata["version"], metadata["source_commit"],
    )
