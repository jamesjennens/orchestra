#!/usr/bin/env python3
"""Acknowledged-plan gate for delegated work (kittrial-pth.21 prototype).

Generic by design: this module knows nothing about a particular worker harness.
The operator supplies an explicit argv; the gate refuses to invoke it until a
plan registration for that exact launch has been claimed and read back from
Beads.

What this is not, and does not claim: there is no OS-level confinement, no
process isolation, no authenticated identity and no exactly-once execution.
A worker or a person can bypass the gate by running the same argv directly, and
a crash after the worker starts can still allow a second execution. The gate
records and re-checks operator discipline; it is not a sandbox. The actor string
is a trusted-team attribution declaration, not a verified identity.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

from client import request
from requirements import ValidationError, canonical_bytes, content_hash, load_json

OK = 0
USAGE = 2          # missing or blank explicit identity/configuration
CLAIM = 3          # task is not in_progress assigned to the actor
ACK = 4            # plan registration comment missing, duplicated or mismatched
PLAN = 5           # plan content does not match the registered content
TRANSPORT = 6      # transport failed or outcome could not be reconciled
WORKER = 7         # the worker itself exited non-zero

KIND = "worker-plan-registration"
RECEIPT_KIND = "worker-gate-receipt"
PREFIX = "Kind: plan-registration.\n"
LIMITS = {
    "exactly_once_execution": False,
    "identity_authenticated": False,
    "os_level_confinement": False,
    "process_isolation": False,
}


class GateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- transport

def _call(config, project, actor, args):
    """One transport call. Any failure is reported as uncertain, never retried."""
    try:
        return request(config, project, actor, list(args))
    except (RuntimeError, OSError, ValueError) as exc:
        raise GateError(TRANSPORT, "transport failed or is uncertain: %s" % (exc,)) from None


def _json_out(result, what):
    if not isinstance(result, dict):
        raise GateError(TRANSPORT, "%s returned no result" % what)
    if result.get("returncode") != 0:
        raise GateError(TRANSPORT, "%s failed (rc=%s): %s"
                        % (what, result.get("returncode"), str(result.get("stderr", ""))[:300]))
    try:
        return json.loads(result.get("stdout", ""))
    except json.JSONDecodeError:
        raise GateError(TRANSPORT, "%s returned an unreadable response" % what) from None


def _comments(config, project, actor, task):
    rows = _json_out(_call(config, project, actor, ["comments", task, "--json"]), "comments")
    if not isinstance(rows, list):
        raise GateError(TRANSPORT, "comments response is not a list")
    return rows


def _issue(config, project, actor, task):
    rows = _json_out(_call(config, project, actor, ["show", task, "--json"]), "show")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('id') != task:
        raise GateError(TRANSPORT, "show response is not an issue")
    return rows[0]


# ------------------------------------------------------------ plan payload

def _plan_bytes(path):
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise GateError(USAGE, "cannot read plan file: %s" % (exc.strerror or exc,)) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise GateError(USAGE, "plan file is not valid UTF-8") from None
    if not text.strip():
        raise GateError(USAGE, 'plan must not be empty')
    return hashlib.sha256(raw).hexdigest(), len(raw), text


def payload_for(project, task, actor, launch_id, plan_sha256, plan_bytes, plan_text, cwd, config_sha256):
    payload = {
        "actor": actor,
        "kind": KIND,
        "launch_id": launch_id,
        "limits": dict(LIMITS),
        "plan": {"bytes": plan_bytes, "sha256": plan_sha256, "text": plan_text},
        "cwd": cwd,
        "config_sha256": config_sha256,
        "project": project,
        "schema_version": 1,
        "task": task,
    }
    payload["sha256"] = content_hash(payload)
    return payload


def body_for(payload):
    """Comment body: one preamble line, then the payload's canonical bytes."""
    return PREFIX + canonical_bytes(payload).decode("utf-8") + "\n"


def parse_body(text):
    """Return the payload only if the comment carries exact canonical bytes."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    rest = text[len(PREFIX):]
    try:
        payload = json.loads(rest)
        if not isinstance(payload, dict) or not isinstance(payload.get('plan'), dict):
            return None
        if canonical_bytes(payload).decode("utf-8") + "\n" != rest:
            return None
        if payload.get("sha256") != content_hash(payload):
            return None
    except (ValidationError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("kind") == KIND else None


def _for_launch(rows, launch_id):
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = parse_body(row.get("text"))
        if payload is not None and payload.get("launch_id") == launch_id:
            out.append((row, payload))
    return out


def _reuse_conflict(rows, launch_id, plan_sha256):
    return [payload for _, payload in _for_launch(rows, launch_id)
            if payload.get("plan", {}).get("sha256") != plan_sha256]


# ---------------------------------------------------------------- receipt

def _write_receipt(path, receipt):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.gate-', delete=False) as f:
            temporary = Path(f.name)
            f.write(canonical_bytes(receipt) + b'\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _exact_match(rows, actor, launch_id, body):
    candidates = _for_launch(rows, launch_id)
    if len(candidates) > 1:
        raise GateError(ACK, 'launch ID has duplicate registrations')
    for row, payload in candidates:
        if row.get('author') != actor or row.get('text') != body or row.get('id') is None or row.get('issue_id', payload['task']) != payload['task']:
            raise GateError(ACK, 'canonical registration does not match exact launch context')
    return candidates


def _expected(args, config, digest, size, text):
    return body_for(payload_for(args.project, args.task, args.actor, args.launch_id,
                    digest, size, text, str(Path(args.cwd).resolve()),
                    hashlib.sha256(canonical_bytes(config)).hexdigest()))


def load_receipt(path):
    try:
        receipt = load_json(path)
    except ValidationError as exc:
        raise GateError(USAGE, "cannot read receipt: %s" % (exc,)) from None
    except OSError as exc:
        raise GateError(USAGE, "cannot read receipt: %s" % (exc.strerror or exc,)) from None
    if not isinstance(receipt, dict) or receipt.get("kind") != RECEIPT_KIND:
        raise GateError(USAGE, "not a worker gate receipt")
    if receipt.get("sha256") != content_hash(receipt):
        raise GateError(PLAN, "receipt content hash does not match")
    return receipt


def _require_identity(args):
    values = [args.config, args.project, args.task, args.actor, args.launch_id, args.plan,
              args.cwd, args.receipt]
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise GateError(USAGE, "explicit config, project, task, actor, launch-id, plan, "
                               "cwd and receipt are required; there are no default identities")
    if not Path(args.cwd).is_dir():
        raise GateError(USAGE, "cwd is not an existing directory")


def _config_of(args):
    try:
        config = load_json(args.config)
    except ValidationError as exc:
        raise GateError(USAGE, "cannot read client config: %s" % (exc,)) from None
    except OSError as exc:
        raise GateError(USAGE, "cannot read client config: %s" % (exc.strerror or exc,)) from None
    if not isinstance(config, dict):
        raise GateError(USAGE, "client config must be an object")
    return config


# ---------------------------------------------------------------- commands

def _claim(config, args):
    """Claim once, then let a read-back decide. Never retry the claim blind."""
    try:
        _call(config, args.project, args.actor, ["update", args.task, "--claim", "--json"])
    except GateError:
        pass  # Uncertain or refused: the read-back below is authoritative.
    issue = _issue(config, args.project, args.actor, args.task)
    state, owner = issue.get("status"), issue.get("assignee")
    if state != "in_progress" or owner != args.actor:
        raise GateError(CLAIM, "task %s is %s assigned to %s, not in_progress assigned to %s"
                        % (args.task, state, owner, args.actor))
    return issue


def cmd_register(args):
    _require_identity(args)
    config = _config_of(args)
    plan_sha256, plan_bytes, plan_text = _plan_bytes(args.plan)
    body = _expected(args, config, plan_sha256, plan_bytes, plan_text)

    rows = _comments(config, args.project, args.actor, args.task)
    if _reuse_conflict(rows, args.launch_id, plan_sha256):
        raise GateError(PLAN, "launch ID %s is already registered with different plan content"
                        % args.launch_id)
    already = _exact_match(rows, args.actor, args.launch_id, body)

    _claim(config, args)

    status = "acknowledged"
    if already:
        comment_id = already[0][0].get("id")
        status = "already-registered"
    else:
        try:
            result = _call(config, args.project, args.actor,
                           ["comments", "add", args.task, body, "--json"])
        except GateError:
            result = None
        if result is not None and result.get("returncode") == 0:
            rows = _comments(config, args.project, args.actor, args.task)
        elif result is not None and result.get("returncode") != 124:
            raise GateError(ACK, "registration comment was refused (rc=%s): %s"
                            % (result.get("returncode"), str(result.get("stderr", ""))[:300]))
        else:
            # Write failed or timed out: reconcile with exactly one read-back.
            rows = _comments(config, args.project, args.actor, args.task)
            status = "reconciled"
        confirmed = _exact_match(rows, args.actor, args.launch_id, body)
        if len(confirmed) != 1:
            raise GateError(TRANSPORT, "registration could not be confirmed by read-back "
                                       "(%d matching comments); not retrying"
                            % (len(confirmed),))
        comment_id = confirmed[0][0].get("id")

    receipt = {
        "actor": args.actor,
        "comment_id": comment_id,
        "config": args.config,
        "cwd": str(Path(args.cwd).resolve()),
        "kind": RECEIPT_KIND,
        "launch_id": args.launch_id,
        "plan": {"bytes": plan_bytes, "path": args.plan, "sha256": plan_sha256},
        "project": args.project,
        "registered_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "schema_version": 1,
        "task": args.task,
    }
    receipt["sha256"] = content_hash(receipt)
    _write_receipt(args.receipt, receipt)
    return {"status": status, "comment_id": comment_id, "launch_id": args.launch_id,
            "plan_sha256": plan_sha256, "receipt": args.receipt}


def _verify(args):
    """Re-check receipt, plan bytes, live canonical comment and live claim."""
    _require_identity(args)
    receipt = load_receipt(args.receipt)
    for field, given in zip(("config", "project", "task", "actor", "launch_id"),
                            (args.config, args.project, args.task, args.actor, args.launch_id)):
        if receipt.get(field) != given:
            raise GateError(USAGE, "receipt %s is %r, not %r" % (field, receipt.get(field), given))
    if receipt.get("plan", {}).get("path") != args.plan:
        raise GateError(USAGE, "receipt was registered for a different plan path")
    if receipt.get("cwd") != str(Path(args.cwd).resolve()):
        raise GateError(USAGE, "receipt was registered for a different cwd")
    config = _config_of(args)

    plan_sha256, plan_bytes, plan_text = _plan_bytes(args.plan)
    if plan_sha256 != receipt["plan"]["sha256"]:
        raise GateError(PLAN, "plan content changed after registration; not launching")
    if plan_bytes != receipt["plan"]["bytes"]:
        raise GateError(PLAN, "plan size changed after registration; not launching")

    rows = _comments(config, args.project, args.actor, args.task)
    if _reuse_conflict(rows, args.launch_id, receipt["plan"]["sha256"]):
        raise GateError(PLAN, "launch ID %s carries different plan content on the server"
                        % args.launch_id)
    confirmed = _exact_match(rows, receipt['actor'], args.launch_id,
                             _expected(args, config, plan_sha256, plan_bytes, plan_text))
    if len(confirmed) != 1:
        raise GateError(ACK, "canonical plan acknowledgement not found exactly once "
                             "(%d matches)" % (len(confirmed),))
    if str(confirmed[0][0]['id']) != str(receipt.get('comment_id')):
        raise GateError(ACK, 'canonical comment ID changed since registration')

    issue = _issue(config, args.project, args.actor, args.task)
    if issue.get("status") != "in_progress" or issue.get("assignee") != args.actor:
        raise GateError(CLAIM, "task %s is %s assigned to %s, not in_progress assigned to %s"
                        % (args.task, issue.get("status"), issue.get("assignee"), args.actor))
    return {"status": "verified", "comment_id": confirmed[0][0].get("id"),
            "launch_id": args.launch_id, "plan_sha256": receipt["plan"]["sha256"],
            "task_status": issue.get("status"), "assignee": issue.get("assignee"),
            "cwd": receipt["cwd"]}


def runner(argv, cwd):
    """The single execution seam: a list argv, never a shell string."""
    return subprocess.run(argv, shell=False, cwd=cwd, check=False)


def cmd_check(args):
    return _verify(args)


def cmd_run(args):
    verified = _verify(args)  # Receipt, plan bytes, comment and claim first.
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise GateError(USAGE, "run needs a worker argv after '--'")
    completed = runner(argv, verified["cwd"])
    code = completed.returncode
    print(json.dumps({"status": "workers-finished" if code == 0 else "worker-failed",
                      "comment_id": verified["comment_id"], "launch_id": args.launch_id,
                      "worker_returncode": code, "argv_length": len(argv)}))
    return OK if code == 0 else WORKER


# ---------------------------------------------------------------- CLI

def _parser():
    parser = argparse.ArgumentParser(
        prog="worker_gate.py",
        description="Register an acknowledged plan, then gate one worker invocation.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "check", "run"):
        part = sub.add_parser(name)
        part.add_argument("--config", required=True)
        part.add_argument("--project", required=True)
        part.add_argument("--task", required=True)
        part.add_argument("--actor", required=True)
        part.add_argument("--launch-id", required=True, dest="launch_id")
        part.add_argument("--plan", required=True)
        part.add_argument("--cwd", required=True)
        part.add_argument("--receipt", required=True)
        if name == "run":
            part.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "register":
            result = cmd_register(args)
        elif args.command == "check":
            result = cmd_check(args)
        else:
            return cmd_run(args)
    except GateError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return exc.code
    except ValidationError as exc:
        print(json.dumps({"status": "refused", "reason": "invalid input: %s" % (exc,)}))
        return USAGE
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({'status': 'refused', 'reason': 'invalid input or operation failed: ' + str(exc)}))
        return USAGE
    print(json.dumps(result))
    return OK


if __name__ == "__main__":
    sys.exit(main())
