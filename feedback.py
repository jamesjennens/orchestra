"""Private, append-only project feedback with cursor paging and idempotent writes."""
import argparse
import base64
import binascii
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

FEED_NAME = ".feedback.jsonl"
SCHEMA_VERSION = 1
MAX_PAGE = 100
MAX_BODY = 12000
MAX_EVIDENCE = 20
MAX_LINK = 2000
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}\Z")
QUARANTINE_SUFFIX = ".incomplete"


def _identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid %s" % field)
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _feed_id(path):
    return hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()


def _watermark(entries):
    if not entries:
        return {"sequence": 0, "digest": ""}
    encoded = json.dumps(entries[-1], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sequence": entries[-1]["sequence"], "digest": hashlib.sha256(encoded).hexdigest()}


def _cursor(value, path, current):
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid feedback cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii") + b"==="))
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
        raise ValueError("Invalid feedback cursor") from None
    if (not isinstance(data, dict) or data.get("v") != 2 or data.get("feed_id") != _feed_id(path)
            or type(data.get("seq")) is not int or data["seq"] < 0
            or not isinstance(data.get("watermark"), dict)):
        raise ValueError("Invalid feedback cursor")
    watermark = data["watermark"]
    if (set(watermark) != {"sequence", "digest"} or type(watermark["sequence"]) is not int
            or watermark["sequence"] < data["seq"]):
        raise ValueError("Invalid feedback cursor")
    current_watermark = _watermark(current)
    if data["seq"] > current_watermark["sequence"] or watermark["sequence"] > current_watermark["sequence"]:
        raise ValueError("Feedback cursor is ahead of the current feed")
    if watermark["sequence"] == current_watermark["sequence"] and watermark["digest"] != current_watermark["digest"]:
        raise ValueError("Feedback cursor does not match the current feed")
    return data["seq"]


def encode_cursor(path, seq, watermark):
    if type(seq) is not int or seq < 0:
        raise ValueError("Invalid feedback sequence")
    raw = json.dumps({"feed_id": _feed_id(path), "seq": seq, "v": 2, "watermark": watermark},
                     sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _entry_id(operation_id):
    return "feedback-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:32]


def _validate_entry(entry, expected_seq=None):
    required = {
        "schema_version", "entry_id", "sequence", "operation_id", "kind", "actor",
        "created_at", "source", "body", "evidence", "triage", "reminder", "supersedes",
    }
    if not isinstance(entry, dict) or set(entry) != required:
        raise ValueError("Invalid feedback entry")
    if entry["schema_version"] != SCHEMA_VERSION or entry["kind"] not in ("feedback", "correction"):
        raise ValueError("Invalid feedback entry schema")
    _identifier(entry["entry_id"], "entry_id")
    _identifier(entry["operation_id"], "operation_id")
    if type(entry["sequence"]) is not int or entry["sequence"] < 1 or (
            expected_seq is not None and entry["sequence"] != expected_seq):
        raise ValueError("Invalid feedback sequence")
    if entry["entry_id"] != _entry_id(entry["operation_id"]):
        raise ValueError("Feedback entry ID does not match operation ID")
    _identifier(entry["actor"], "actor")
    _timestamp(entry["created_at"])
    source = entry["source"]
    if not isinstance(source, dict) or set(source) != {"task", "version"}:
        raise ValueError("Feedback source must contain task and version")
    if source["task"] is not None:
        _identifier(source["task"], "source task")
    if not isinstance(source["version"], str) or len(source["version"]) > 200:
        raise ValueError("Invalid source version")
    if not isinstance(entry["body"], str) or not entry["body"].strip() or len(entry["body"]) > MAX_BODY:
        raise ValueError("Feedback body must be bounded and nonempty")
    if (not isinstance(entry["evidence"], list) or len(entry["evidence"]) > MAX_EVIDENCE or
            any(not isinstance(link, str) or not link.strip() or len(link) > MAX_LINK
                for link in entry["evidence"])):
        raise ValueError("Invalid evidence links")
    if not isinstance(entry["triage"], dict) or set(entry["triage"]) != {"task", "label"}:
        raise ValueError("Invalid triage link")
    if entry["triage"]["task"] is not None:
        _identifier(entry["triage"]["task"], "triage task")
    if not isinstance(entry["triage"]["label"], str) or len(entry["triage"]["label"]) > 200:
        raise ValueError("Invalid triage label")
    if not isinstance(entry["reminder"], dict) or set(entry["reminder"]) != {"kind", "text"}:
        raise ValueError("Invalid reminder")
    if entry["reminder"]["kind"] not in ("none", "checkpoint", "handoff"):
        raise ValueError("Invalid reminder kind")
    if not isinstance(entry["reminder"]["text"], str) or len(entry["reminder"]["text"]) > 1000:
        raise ValueError("Invalid reminder text")
    if entry["kind"] == "feedback" and entry["supersedes"] is not None:
        raise ValueError("Feedback entry cannot supersede another entry")
    if entry["kind"] == "correction":
        if not isinstance(entry["supersedes"], str):
            raise ValueError("Correction target is required")
    return entry


def validate_feed_text(text):
    if not isinstance(text, str):
        raise ValueError("Feedback feed must be text")
    previous = 0
    operations = {}
    entries = []
    for line in text.splitlines():
        if not line.strip():
            raise ValueError("Feedback feed contains a blank line")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError("Feedback feed contains invalid JSON") from None
        _validate_entry(entry, previous + 1)
        if entry["kind"] == "correction" and entry["supersedes"] not in {
                item["entry_id"] for item in entries}:
            raise ValueError("Correction target must precede the correction")
        if entry["operation_id"] in operations and operations[entry["operation_id"]] != entry:
            raise ValueError("Duplicate feedback operation has conflicting content")
        operations[entry["operation_id"]] = entry
        entries.append(entry)
        previous = entry["sequence"]
    return entries


def _assert_regular(path):
    if path.is_symlink():
        raise ValueError("Feedback feed must not be a symlink")


def _read(path):
    _assert_regular(path)
    if not path.exists():
        return []
    raw = path.read_bytes()
    try:
        return validate_feed_text(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        if not raw or raw.endswith(b"\n") or raw.endswith(b"\r"):
            raise
        lines = raw.splitlines(keepends=True)
        if len(lines) < 2:
            raise
        prefix = b"".join(lines[:-1])
        try:
            entries = validate_feed_text(prefix.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise
        quarantine = path.with_name(path.name + "." + hashlib.sha256(lines[-1]).hexdigest()[:16] + QUARANTINE_SUFFIX)
        if quarantine.exists() or quarantine.is_symlink():
            raise ValueError("Feedback recovery quarantine path already exists")
        quarantine.write_bytes(lines[-1])
        path.write_bytes(prefix)
        return entries


def _write_append(path, entry):
    _assert_regular(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _payload(args, attachments):
    if len(args) != 2 or args[0] not in ("add", "correct") or not args[1].startswith("@attachment:"):
        raise ValueError("feedback add/correct requires --file JSON payload")
    item = attachments.get(args[1].partition(":")[2], {})
    if item.get("flag") not in ("--file", "-f") or not isinstance(item.get("text"), str):
        raise ValueError("Invalid feedback attachment")
    try:
        payload = json.loads(item["text"])
    except json.JSONDecodeError:
        raise ValueError("Feedback payload must be JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("Feedback payload must be an object")
    return payload


def add(path, actor, payload, kind="feedback"):
    required = {"operation_id", "created_at", "body", "source", "evidence", "triage", "reminder", "supersedes"}
    if set(payload) != required:
        raise ValueError("Invalid feedback fields")
    operation_id = _identifier(payload["operation_id"], "operation_id")
    entries = _read(path)
    existing = next((item for item in entries if item["operation_id"] == operation_id), None)
    if existing:
        expected = _make_entry(existing["sequence"], actor, payload, kind)
        if existing != expected:
            raise ValueError("Feedback operation ID already used with different content")
        return {"entry": existing, "reconciled": True}
    if kind == "correction":
        target = payload["supersedes"]
        if not isinstance(target, str) or not any(item["entry_id"] == target for item in entries):
            raise ValueError("Correction target must reference an existing feedback entry")
    entry = _make_entry(len(entries) + 1, actor, payload, kind)
    _validate_entry(entry, len(entries) + 1)
    _write_append(path, entry)
    return {"entry": entry, "reconciled": False}


def _make_entry(sequence, actor, payload, kind):
    operation_id = payload["operation_id"]
    return {
        "schema_version": SCHEMA_VERSION,
        "entry_id": _entry_id(operation_id),
        "sequence": sequence,
        "operation_id": operation_id,
        "kind": kind,
        "actor": actor,
        "created_at": payload["created_at"],
        "source": payload["source"],
        "body": payload["body"],
        "evidence": payload["evidence"],
        "triage": payload["triage"],
        "reminder": payload["reminder"],
        "supersedes": payload["supersedes"] if kind == "correction" else None,
    }


def list_entries(path, limit=20, cursor=None):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("Feedback limit must be 1..%d" % MAX_PAGE)
    current = _read(path)
    start = _cursor(cursor, path, current) if cursor is not None else 0
    available = [entry for entry in current if entry["sequence"] > start]
    page = available[:limit]
    watermark = _watermark(current)
    return {
        "entries": page,
        "next_cursor": encode_cursor(path, page[-1]["sequence"], watermark) if len(available) > len(page) else None,
        "watermark": watermark,
        "count": len(page),
    }


def execute(path, actor, args, attachments):
    if not isinstance(args, list) or not args:
        raise ValueError("Use feedback add, correct or list")
    if args[0] == "list":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("list")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--cursor")
        parsed = parser.parse_args(args)
        return list_entries(path, parsed.limit, parsed.cursor)
    payload = _payload(args, attachments)
    return add(path, actor, payload, "correction" if args[0] == "correct" else "feedback")
