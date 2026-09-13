#!/usr/bin/env python3
"""Offline BRD publisher, contract version 1.

Implements the publisher slice of docs/REQUIREMENTS_CONTRACT.md: consume one
validated schema_version 1 manifest plus an optional acceptance object, render
BRD.md, write an exact-byte receipt, and advance the root current.json pointer
only after a complete named publication exists. Everything is offline: no
server access, no schema migration and no requirement revision is written.

Failure mapping (the contract permits ValueError or OSError):

* ValidationError (a subclass of ValueError) -- malformed manifest, malformed
  acceptance object or malformed JSON/UTF-8 at the file boundary.
* ValueError -- policy refusals: a reserved or non-portable baseline name, a
  symbolic-link publication path, a tampered existing publication, or a
  conflicting retry for an existing publication name.
* OSError -- filesystem problems: the output path is not a directory, or a
  write, rename or atomic pointer replacement fails.

Publication layout, where OUTPUT is the --output directory and BASELINE is
manifest["baseline"]:

    OUTPUT/BASELINE/manifest.json     canonical bytes of the manifest
    OUTPUT/BASELINE/BRD.md            rendered narrative and requirements
    OUTPUT/BASELINE/acceptance.json   only for an accepted manifest
    OUTPUT/BASELINE/receipt.json      exact-byte hashes of the above files
    OUTPUT/current.json               last successful explicit publication

JSON artifacts are the canonical UTF-8 form (sorted keys, ensure_ascii=False,
comma/colon separators, no line breaks, no BOM). Markdown is UTF-8 with LF line
endings and a single trailing LF. No timestamp, absolute path or other
machine-local value is rendered, so identical input yields identical bytes.

current.json records local publication evidence only. It is not server
acknowledgement and not owner acceptance of the baseline.
"""

import argparse
import hashlib
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path

import requirements
from requirements import canonical_bytes, load_json, validate_manifest

CURRENT_NAME = "current.json"
RECEIPT_NAME = "receipt.json"
MANIFEST_NAME = "manifest.json"
BRD_NAME = "BRD.md"
ACCEPTANCE_NAME = "acceptance.json"

RECEIPT_FIELDS = ("schema_version", "baseline", "manifest_sha256", "state", "files")

# Baselines whose name would collide with a file this publisher owns in the
# output root. Compared case-insensitively because Windows paths are.
RESERVED_BASELINES = (CURRENT_NAME,)

# Windows device names are unusable as directory components on that platform,
# so they are refused everywhere to keep publications portable.
WINDOWS_DEVICE_NAMES = ("con", "prn", "aux", "nul")
WINDOWS_NUMBERED_DEVICE = re.compile(r"(com|lpt)[1-9]\Z")
NON_PORTABLE_ENDING = re.compile(r"[. ]\Z")

DRAFT_BANNER = (
    "> **Baseline state: DRAFT.** No acceptance decision is recorded for this content."
)
ACCEPTED_BANNER = "> **Baseline state: ACCEPTED.**"
BANNER_BY_STATE = {"draft": DRAFT_BANNER, "accepted": ACCEPTED_BANNER}

# A pointer replacement is idempotent (it always installs the same bytes for a
# given publication), so a bounded retry is safe when another writer or an
# antivirus filter momentarily holds current.json open on Windows.
POINTER_ATTEMPTS = 6
POINTER_RETRY_SECONDS = 0.02


def sha256_bytes(data):
    """Lowercase SHA-256 of exact bytes."""
    return hashlib.sha256(data).hexdigest()


def _is_reparse_directory(path):
    """True for a Windows junction or mount point (no isjunction below 3.12)."""
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not flag:
        return False
    try:
        if not getattr(path.lstat(), "st_file_attributes", 0) & flag:
            return False
        # Only directories are treated as links: a junction or directory
        # symlink redirects writes, while a file placeholder does not.
        return path.is_dir()
    except OSError:
        return False


def _is_link(path):
    """True for a symbolic link, a Windows junction or a mount point."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            return bool(isjunction(path))
        except OSError:
            return False
    return _is_reparse_directory(path)


def _reject_link(path, what):
    if _is_link(path):
        raise ValueError("%s must not be a symbolic link: %s" % (what, path))


def _check_baseline_name(name):
    """Refuse names that are unsafe as a directory component of a publication."""
    if not isinstance(name, str) or not requirements.BASELINE_NAME.match(name):
        raise ValueError("baseline is not a safe directory component: %r" % (name,))
    if name.lower() in RESERVED_BASELINES:
        raise ValueError("baseline name is reserved by the publisher: %r" % (name,))
    stem = name.split(".", 1)[0].lower()
    if stem in WINDOWS_DEVICE_NAMES or WINDOWS_NUMBERED_DEVICE.match(stem):
        raise ValueError("baseline name is a reserved device name: %r" % (name,))
    if NON_PORTABLE_ENDING.search(name):
        raise ValueError("baseline name must not end with a dot or space: %r" % (name,))


def _render_record(lines, record):
    lines.append("### %s" % record["title"])
    lines.append("")
    if "key" in record:
        lines.append("- Key: %s" % record["key"])
    lines.append("- Source ID: %s" % record["id"])
    lines.append("- Revision: %d" % record["revision"])
    lines.append("- Acceptance state: %s" % record["acceptance_state"])
    lines.append("- SHA-256: %s" % record["sha256"])
    for reference in record.get("references", []):
        lines.append(
            "- Reference: %s@%d %s" % (reference["id"], reference["revision"], reference["sha256"])
        )
    lines.append("")
    # Preserve selected text; render_brd normalizes only line endings.
    lines.append(record["description"])
    if not record["description"].endswith("\n"):
        lines.append("")


def render_brd(manifest, acceptance=None):
    """Render the published Markdown document for one validated manifest."""
    state = manifest["state"]
    lines = []
    lines.append("# Business requirements document: %s" % manifest["baseline"])
    lines.append("")
    lines.append(BANNER_BY_STATE[state])
    lines.append(">")
    lines.append(
        "> Generated offline from one validated manifest; reproducible from its recorded"
        " content hashes. current.json records local publication only, not server"
        " acknowledgement or owner acceptance."
    )
    lines.append("")
    lines.append("- Canonical project: %s" % manifest["canonical_project"])
    lines.append("- Job: %s" % manifest["job"])
    lines.append("- Baseline: %s" % manifest["baseline"])
    lines.append("- Content state: %s" % state)
    lines.append("- Manifest SHA-256: %s" % manifest["sha256"])
    lines.append("- Hash convention: %s" % manifest["hash_convention"])
    lines.append("- Authority: %s" % manifest["authority"])
    lines.append("")
    if acceptance is not None:
        lines.append("## Acceptance")
        lines.append("")
        lines.append("- Decision: %s" % acceptance["decision_id"])
        lines.append("- Owners: %s" % ", ".join(acceptance["owners"]))
        lines.append("- Approvers: %s" % ", ".join(acceptance["approvers"]))
        lines.append("- Policy: %s" % acceptance["policy"])
        lines.append("- Evidence: %s" % acceptance["evidence"])
        lines.append("")
    lines.append("## Narrative")
    lines.append("")
    for record in manifest["narrative"]:
        _render_record(lines, record)
    lines.append("## Requirements")
    lines.append("")
    for record in manifest["requirements"]:
        _render_record(lines, record)
    text = "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def _build_files(manifest, acceptance):
    """Exact bytes of every artifact in the named publication directory."""
    files = {
        MANIFEST_NAME: canonical_bytes(manifest),
        BRD_NAME: render_brd(manifest, acceptance).encode("utf-8"),
    }
    if acceptance is not None:
        files[ACCEPTANCE_NAME] = canonical_bytes(acceptance)
    receipt = {
        "schema_version": manifest["schema_version"],
        "baseline": manifest["baseline"],
        "manifest_sha256": manifest["sha256"],
        "state": manifest["state"],
        "files": {name: sha256_bytes(data) for name, data in sorted(files.items())},
    }
    files[RECEIPT_NAME] = canonical_bytes(receipt)
    return files


def _write_bytes(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_artifacts(directory, files):
    for name in sorted(files):
        _write_bytes(directory / name, files[name])


def _rename_into_place(staging, target):
    os.rename(str(staging), str(target))


def _atomic_replace(source, destination):
    os.replace(str(source), str(destination))


def _remove_tree(path):
    """Best-effort removal of one staging directory we created."""
    try:
        for entry in path.iterdir():
            if entry.is_dir() and not _is_link(entry):
                _remove_tree(entry)
            else:
                os.unlink(entry)
        os.rmdir(path)
    except OSError:
        pass


def _check_receipt(receipt_path, directory):
    """Verify a persisted receipt against the artifact bytes beside it."""
    receipt = load_json(receipt_path)
    if not isinstance(receipt, dict):
        raise ValueError("%s must be a JSON object" % RECEIPT_NAME)
    if sorted(receipt) != sorted(RECEIPT_FIELDS):
        raise ValueError("%s has unexpected fields" % RECEIPT_NAME)
    listed = receipt["files"]
    if not isinstance(listed, dict) or not listed:
        raise ValueError("%s.files must be a nonempty object" % RECEIPT_NAME)
    if RECEIPT_NAME in listed:
        raise ValueError("%s must not hash itself" % RECEIPT_NAME)
    for name in sorted(listed):
        digest = listed[name]
        if Path(name).name != name:
            raise ValueError("%s.files lists an unsafe name: %r" % (RECEIPT_NAME, name))
        if not isinstance(digest, str) or not requirements.SHA256_TEXT.match(digest):
            raise ValueError("%s.files[%s] is not a lowercase SHA-256" % (RECEIPT_NAME, name))
        artifact = directory / name
        if _is_link(artifact) or not artifact.is_file():
            raise ValueError("%s.files lists a missing artifact: %s" % (RECEIPT_NAME, name))
        if sha256_bytes(artifact.read_bytes()) != digest:
            raise ValueError("%s.files[%s] does not match the artifact bytes" % (RECEIPT_NAME, name))
    return receipt


def _verify_existing(target, files):
    """Confirm an existing named publication is byte-identical to this one."""
    present = {}
    for entry in sorted(target.iterdir(), key=lambda item: item.name):
        if _is_link(entry):
            raise ValueError("existing publication contains a symbolic link: %s" % entry.name)
        if not entry.is_file():
            raise ValueError("existing publication contains an unexpected entry: %s" % entry.name)
        present[entry.name] = entry.read_bytes()
    if set(present) != set(files):
        raise ValueError(
            "existing publication %s has unexpected files: %s"
            % (target.name, ", ".join(sorted(present)))
        )
    _check_receipt(target / RECEIPT_NAME, target)
    for name in sorted(files):
        if present[name] != files[name]:
            raise ValueError(
                "existing publication %s conflicts with this publication: %s differs"
                % (target.name, name)
            )
    return present


def _prepare_output_root(output):
    root = Path(output)
    # Check before resolving: resolve() would hide redirects in parent paths.
    for component in (root.absolute(), *root.absolute().parents):
        _reject_link(component, "output path component")
    if root.exists():
        if not root.is_dir():
            raise OSError("output path exists and is not a directory: %s" % root)
    else:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A concurrent writer may have created it between the two checks.
            if not root.is_dir():
                raise
        _reject_link(root, "output directory")
    return root


def _install_pointer(temporary, pointer_path):
    """Atomically replace the current pointer, tolerating a transient lock."""
    delay = POINTER_RETRY_SECONDS
    for attempt in range(POINTER_ATTEMPTS):
        try:
            _atomic_replace(temporary, pointer_path)
            return
        except OSError:
            if attempt == POINTER_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _write_pointer(root, manifest, receipt_bytes):
    pointer_path = root / CURRENT_NAME
    _reject_link(pointer_path, "current pointer")
    pointer_bytes = canonical_bytes(
        {
            "schema_version": manifest["schema_version"],
            "baseline": manifest["baseline"],
            "manifest_sha256": manifest["sha256"],
            "receipt_sha256": sha256_bytes(receipt_bytes),
        }
    )
    temporary = root / (".current-%s.tmp" % secrets.token_hex(8))
    _write_bytes(temporary, pointer_bytes)
    try:
        _install_pointer(temporary, pointer_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return pointer_path


def publish(manifest, output, acceptance=None):
    """Publish one validated manifest offline and return the publication Path."""
    validate_manifest(manifest, acceptance)
    _check_baseline_name(manifest["baseline"])
    files = _build_files(manifest, acceptance)
    root = _prepare_output_root(output)
    target = root / manifest["baseline"]
    _reject_link(target, "publication directory")

    if target.exists():
        if not target.is_dir():
            raise OSError("publication path exists and is not a directory: %s" % target)
        _verify_existing(target, files)
        _write_pointer(root, manifest, files[RECEIPT_NAME])
        return target

    staging = root / (".staging-%s-%s" % (manifest["baseline"], secrets.token_hex(8)))
    _reject_link(staging, "staging directory")
    staging.mkdir()
    renamed = False
    try:
        _write_artifacts(staging, files)
        _verify_existing(staging, files)
        try:
            _rename_into_place(staging, target)
            renamed = True
        except OSError:
            # A racing writer may have installed the same name first. Agreeing
            # exactly is acceptable; anything else is a conflicting publication.
            if _is_link(target) or not target.is_dir():
                raise
            _verify_existing(target, files)
            _write_pointer(root, manifest, files[RECEIPT_NAME])
            return target
    finally:
        # Interrupted staging is never current: drop anything we did not rename.
        if not renamed:
            _remove_tree(staging)
    _write_pointer(root, manifest, files[RECEIPT_NAME])
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Publish one validated schema_version 1 requirement manifest offline."
    )
    parser.add_argument("manifest", help="path to the manifest JSON file")
    parser.add_argument("--output", required=True, help="output directory for publications")
    parser.add_argument("--acceptance", help="path to the acceptance JSON file")
    args = parser.parse_args(argv)
    try:
        manifest = load_json(args.manifest)
        acceptance = load_json(args.acceptance) if args.acceptance else None
        target = publish(manifest, args.output, acceptance)
    except OSError as exc:
        print("publication failed: %s" % (exc.strerror or exc,), file=sys.stderr)
        return 1
    except ValueError as exc:
        print("publication failed: %s" % (exc,), file=sys.stderr)
        return 1
    print("published %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
