"""Offline requirement manifest validator, contract version 1.

Implements the validator slice of docs/REQUIREMENTS_CONTRACT.md: canonical
bytes, content hashing, structural validation of one schema_version 1
manifest and optional acceptance object. No server access, no schema
migration and no requirement revision is written.
"""

import argparse
import hashlib
import json
import re
import sys

BASELINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
SHA256_TEXT = re.compile(r"[0-9a-f]{64}\Z")
STATES = ("draft", "accepted")
POLICIES = ("any-owner", "all-owners")

MANIFEST_FIELDS = (
    "schema_version",
    "baseline",
    "state",
    "canonical_project",
    "job",
    "authority",
    "hash_convention",
    "narrative",
    "requirements",
    "sha256",
)
RECORD_FIELDS = ("id", "title", "description", "revision", "acceptance_state", "sha256")
REQUIREMENT_FIELDS = RECORD_FIELDS + ("key",)
REFERENCE_FIELDS = ("id", "revision", "sha256")
ACCEPTANCE_FIELDS = (
    "manifest_sha256",
    "decision_id",
    "owners",
    "approvers",
    "policy",
    "evidence",
)


class ValidationError(ValueError):
    """Raised when manifest or acceptance input is malformed."""


def canonical_bytes(value):
    """UTF-8 JSON, sorted keys, ensure_ascii=False, comma/colon separators."""
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return text.encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("value is not canonical JSON: %s" % (exc,))


def content_hash(mapping):
    """SHA-256 of canonical bytes, excluding only top-level sha256."""
    if not isinstance(mapping, dict):
        raise ValidationError("content_hash requires a mapping")
    body = {key: value for key, value in mapping.items() if key != "sha256"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _fail(message):
    raise ValidationError(message)


def _mapping(value, where):
    if not isinstance(value, dict):
        _fail("%s must be an object" % where)
    return value


def _nonempty_string(value, where):
    if not isinstance(value, str) or not value.strip():
        _fail("%s must be a nonempty string" % where)
    return value


def _fields(record, allowed, required, where):
    extra = sorted(set(record) - set(allowed))
    if extra:
        _fail("%s has unknown field(s): %s" % (where, ", ".join(extra)))
    for name in required:
        if name not in record:
            _fail("%s is missing field '%s'" % (where, name))


def _revision(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail("%s must be a positive integer (not boolean)" % where)
    return value


def _checked_hash(value, where):
    if not isinstance(value, str) or not SHA256_TEXT.match(value):
        _fail("%s must be a lowercase 64-character SHA-256" % where)
    return value


def _name_list(value, where):
    if not isinstance(value, list) or not value:
        _fail("%s must be a nonempty list" % where)
    names = []
    for index, name in enumerate(value):
        names.append(_nonempty_string(name, "%s[%d]" % (where, index)))
    if len(set(names)) != len(names):
        _fail("%s has duplicate entries" % where)
    return names


def _validate_record(record, where, has_key):
    required = REQUIREMENT_FIELDS if has_key else RECORD_FIELDS
    allowed = required + ("references",)
    _mapping(record, where)
    _fields(record, allowed, required, where)
    _nonempty_string(record["id"], "%s.id" % where)
    _nonempty_string(record["title"], "%s.title" % where)
    _nonempty_string(record["description"], "%s.description" % where)
    if has_key:
        _nonempty_string(record["key"], "%s.key" % where)
    _revision(record["revision"], "%s.revision" % where)
    if record["acceptance_state"] not in STATES:
        _fail(
            "%s.acceptance_state must be one of %s" % (where, ", ".join(STATES))
        )
    _checked_hash(record["sha256"], "%s.sha256" % where)
    if content_hash(record) != record["sha256"]:
        _fail("%s.sha256 does not match its content" % where)
    if "references" in record and not isinstance(record["references"], list):
        _fail("%s.references must be a list" % where)


def _validate_references(records, index):
    for record in records:
        for position, reference in enumerate(record.get("references", [])):
            where = "%s.references[%d]" % (record["id"], position)
            _mapping(reference, where)
            _fields(reference, REFERENCE_FIELDS, REFERENCE_FIELDS, where)
            target_id = _nonempty_string(reference["id"], "%s.id" % where)
            _revision(reference["revision"], "%s.revision" % where)
            _checked_hash(reference["sha256"], "%s.sha256" % where)
        seen = []
        for reference in record.get("references", []):
            token = (reference["id"], reference["revision"], reference["sha256"])
            if token in seen:
                _fail("%s.references repeats a reference" % record["id"])
            seen.append(token)
            target = index.get(reference["id"])
            if target is None:
                _fail(
                    "%s.references targets unknown record '%s'"
                    % (record["id"], reference["id"])
                )
            if reference["revision"] != target["revision"]:
                _fail(
                    "%s.references revision does not match '%s'"
                    % (record["id"], reference["id"])
                )
            if reference["sha256"] != target["sha256"]:
                _fail(
                    "%s.references sha256 does not match '%s'"
                    % (record["id"], reference["id"])
                )


def _validate_acceptance(acceptance, manifest):
    _mapping(acceptance, "acceptance")
    _fields(acceptance, ACCEPTANCE_FIELDS, ACCEPTANCE_FIELDS, "acceptance")
    _checked_hash(acceptance["manifest_sha256"], "acceptance.manifest_sha256")
    if acceptance["manifest_sha256"] != manifest["sha256"]:
        _fail("acceptance.manifest_sha256 does not match the manifest")
    _nonempty_string(acceptance["decision_id"], "acceptance.decision_id")
    owners = _name_list(acceptance["owners"], "acceptance.owners")
    approvers = _name_list(acceptance["approvers"], "acceptance.approvers")
    if acceptance["policy"] not in POLICIES:
        _fail("acceptance.policy must be one of %s" % ", ".join(POLICIES))
    _nonempty_string(acceptance["evidence"], "acceptance.evidence")
    if not set(approvers) <= set(owners):
        _fail("acceptance.approvers must be a subset of acceptance.owners")
    if acceptance["policy"] == "all-owners" and set(owners) != set(approvers):
        _fail("acceptance.policy all-owners requires identical owners and approvers")


def validate_manifest(manifest, acceptance=None):
    """Return None for a valid manifest, else raise ValidationError."""
    _mapping(manifest, "manifest")
    _fields(manifest, MANIFEST_FIELDS, MANIFEST_FIELDS, "manifest")
    if isinstance(manifest["schema_version"], bool) or not isinstance(
        manifest["schema_version"], int
    ) or manifest["schema_version"] != 1:
        _fail("manifest.schema_version must be the integer 1")
    for name in ("baseline", "canonical_project", "job", "authority", "hash_convention"):
        _nonempty_string(manifest[name], "manifest.%s" % name)
    if not BASELINE_NAME.match(manifest["baseline"]):
        _fail("manifest.baseline is not a safe directory component")
    if manifest["state"] not in STATES:
        _fail("manifest.state must be one of %s" % ", ".join(STATES))
    if not isinstance(manifest["narrative"], list):
        _fail("manifest.narrative must be a list")
    if not isinstance(manifest["requirements"], list) or not manifest["requirements"]:
        _fail("manifest.requirements must be a nonempty list")

    records = []
    index = {}
    keys = []
    for position, record in enumerate(manifest["narrative"]):
        _validate_record(record, "narrative[%d]" % position, has_key=False)
        records.append(record)
    for position, record in enumerate(manifest["requirements"]):
        _validate_record(record, "requirements[%d]" % position, has_key=True)
        records.append(record)
        keys.append(record["key"])
    for record in records:
        if record["id"] in index:
            _fail("manifest has duplicate record id '%s'" % record["id"])
        index[record["id"]] = record
    if len(set(keys)) != len(keys):
        _fail("manifest has duplicate requirement key")

    _validate_references(records, index)

    _checked_hash(manifest["sha256"], "manifest.sha256")
    if content_hash(manifest) != manifest["sha256"]:
        _fail("manifest.sha256 does not match its content")

    if manifest["state"] == "draft":
        if acceptance is not None:
            _fail("a draft manifest must not carry an acceptance object")
        return None
    for record in records:
        if record["acceptance_state"] != "accepted":
            _fail(
                "record '%s' must be accepted for an accepted manifest"
                % record["id"]
            )
    if acceptance is None:
        _fail("an accepted manifest requires an acceptance object")
    _validate_acceptance(acceptance, manifest)
    return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON field: %s" % key)
        result[key] = value
    return result


def load_json(path):
    """Read UTF-8 JSON without silently discarding duplicate fields."""
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        _fail("cannot read %s: %s" % (path, exc.strerror or exc))
    try:
        result = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        canonical_bytes(result)  # Reject nonfinite numbers and invalid Unicode.
        return result
    except UnicodeDecodeError:
        _fail("cannot read %s: not valid UTF-8" % path)
    except json.JSONDecodeError as exc:
        _fail("cannot parse %s as JSON: %s" % (path, exc))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate one schema_version 1 requirement manifest."
    )
    parser.add_argument("manifest", help="path to the manifest JSON file")
    parser.add_argument("--acceptance", help="path to the acceptance JSON file")
    args = parser.parse_args(argv)
    try:
        manifest = load_json(args.manifest)
        acceptance = load_json(args.acceptance) if args.acceptance else None
        validate_manifest(manifest, acceptance)
    except ValidationError as exc:
        print("invalid manifest: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
