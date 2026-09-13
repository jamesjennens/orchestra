import hashlib
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker_gate
from worker_gate import main

TASK = "kittrial-pth.21"
ACTOR = "hermes-deepseek-barrier"
LAUNCH = "kittrial-pth.21-20260913T162912Z-hermes-deepseek-barrier-0000ffff"
PLAN_TEXT = "# plan\nGate worker argv behind an acknowledged plan.\n"
HOSTILE = ["python.exe", "print('a; rm -rf /')", "$(touch pwned)", "`id`", "a|b & c",
           "line\nbreak", "  spaced  ", "worker*argv", "quote'\""]


def ok(stdout):
    return {"returncode": 0, "stdout": stdout, "stderr": ""}


class FakeServer:
    """Mock of the Beads endpoint surface the gate is allowed to use."""

    def __init__(self):
        self.comments = []
        self.issue = {"id": TASK, "status": "open", "assignee": None}
        self.calls = []
        self.uncertain_add = None  # None | "lost" (not written) | "landed" (written then lost)

    def adds(self):
        return [c for c in self.calls if len(c) > 1 and c[0] == "comments" and c[1] == "add"]

    def __call__(self, config, project, actor, args):
        self.calls.append(list(args))
        if args[:1] == ["comments"] and args[1:2] == ["add"]:
            if self.uncertain_add == "lost":
                raise RuntimeError("SSH timed out; outcome may be uncertain. Inspect state.")
            self.comments.append({"id": "c%d" % (len(self.comments) + 1), "issue_id": args[2],
                                  "author": actor, "text": args[3],
                                  "created_at": "2026-09-13T16:29:12Z"})
            if self.uncertain_add == "landed":
                raise RuntimeError("Invalid endpoint response; inspect state before retrying.")
            return ok(json.dumps({"id": self.comments[-1]["id"]}))
        if args[:2] == ["comments", TASK]:
            return ok(json.dumps(self.comments))
        if args[:1] == ["show"]:
            return ok(json.dumps([dict(self.issue)]))
        if args[:1] == ["update"] and "--claim" in args:
            self.issue["status"] = "in_progress"
            self.issue["assignee"] = actor
            return ok(json.dumps([dict(self.issue)]))
        raise AssertionError("unexpected transport args: %r" % (args,))


class GateTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan = self.root / "plan.md"
        self.plan.write_text(PLAN_TEXT, encoding="utf-8")
        self.config = self.root / "client.json"
        self.config.write_text(json.dumps({"host": "sample", "endpoint": "/srv/kit/endpoint.py",
                                           "root": "/srv/state"}), encoding="utf-8")
        self.receipt = self.root / "receipt.json"
        self.server = FakeServer()
        self.patch = patch.object(worker_gate, "request", side_effect=self.server)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def argv(self, command, *extra):
        return [command, "--config", str(self.config), "--project", "kittrial",
                "--task", TASK, "--actor", ACTOR, "--launch-id", LAUNCH,
                "--plan", str(self.plan), "--cwd", str(self.root),
                "--receipt", str(self.receipt), *extra]

    def run_gate(self, command, *extra):
        argv = self.argv(command, *extra)
        with patch.object(worker_gate, "runner",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as runner:
            code = main(argv)
            return code, runner

    # ------------------------------------------------------------ happy path

    def test_register_then_run_executes_only_after_acknowledgement(self):
        with patch.object(worker_gate.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as spawn:
            self.assertEqual(main(self.argv("register")), 0)
            receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt["plan"]["sha256"],
                             hashlib.sha256(self.plan.read_bytes()).hexdigest())
            for field in ("config", "project", "task", "actor", "launch_id", "cwd"):
                self.assertTrue(receipt[field])
            self.assertEqual(receipt["task"], TASK)
            self.assertEqual(receipt["actor"], ACTOR)
            self.assertEqual(receipt["launch_id"], LAUNCH)
            self.assertEqual(receipt["sha256"],
                             worker_gate.content_hash(receipt))
            self.assertEqual(self.server.issue["status"], "in_progress")
            self.assertEqual(len(self.server.comments), 1)
            self.assertEqual(main(self.argv("check")), 0)
            self.assertEqual(spawn.call_count, 0)
            self.assertEqual(main(self.argv("run", "--", *HOSTILE)), 0)

        self.assertEqual(spawn.call_count, 1)
        argv, kwargs = spawn.call_args.args[0], spawn.call_args.kwargs
        self.assertEqual(argv, HOSTILE)
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["cwd"], str(self.root))

    def test_worker_argv_never_enters_transport(self):
        with patch.object(worker_gate.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")):
            main(self.argv("register"))
            main(self.argv("run", "--", *HOSTILE))
        for call in self.server.calls:
            joined = " ".join(call)
            for token in HOSTILE:
                self.assertNotIn(token, joined)
        self.assertEqual([c[0] for c in self.server.calls if c[0] == "update"],
                         ["update"])  # claimed once, never again from run

    def test_body_is_exact_canonical_bytes_and_rejects_tampering(self):
        main(self.argv("register"))
        payload = worker_gate.parse_body(self.server.comments[0]["text"])
        self.assertIsNotNone(payload)
        self.assertEqual(payload['plan']['text'].encode('utf-8'),self.plan.read_bytes())
        self.assertEqual(payload["sha256"], worker_gate.content_hash(payload))
        text = self.server.comments[0]["text"]
        self.assertIsNone(worker_gate.parse_body(text.replace('"bytes":', '"bytes" :')))
        broken = json.loads(text[len(worker_gate.PREFIX):])
        broken["plan"]["sha256"] = "0" * 64
        self.assertIsNone(worker_gate.parse_body(
            worker_gate.PREFIX + json.dumps(broken, sort_keys=True) + "\n"))

    # ------------------------------------------------------- no execution

    def test_no_acknowledgement_means_no_execution(self):
        main(self.argv("register"))
        self.server.comments.clear()
        code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, worker_gate.ACK)
        runner.assert_not_called()

    def test_foreign_author_means_no_execution(self):
        main(self.argv("register"))
        self.server.comments[0]["author"] = "someone-else"
        code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, worker_gate.ACK)
        runner.assert_not_called()

    def test_changed_plan_means_no_execution(self):
        main(self.argv("register"))
        self.plan.write_text(PLAN_TEXT + "extra instruction\n", encoding="utf-8")
        code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, worker_gate.PLAN)
        runner.assert_not_called()

    def test_changed_config_destination_means_no_execution(self):
        main(self.argv('register'))
        config=json.loads(self.config.read_text());config['host']='another-server'
        self.config.write_text(json.dumps(config))
        code,runner=self.run_gate('run','--','python','-c','x')
        self.assertEqual(code,worker_gate.ACK);runner.assert_not_called()

    def test_rehashed_wrong_context_and_replaced_comment_id_refused(self):
        main(self.argv('register'))
        original=self.server.comments[0]['text']
        payload=worker_gate.parse_body(original);payload['project']='other'
        payload['sha256']=worker_gate.content_hash(payload)
        self.server.comments[0]['text']=worker_gate.body_for(payload)
        code,runner=self.run_gate('run','--','python','-c','x')
        self.assertEqual(code,worker_gate.ACK);runner.assert_not_called()
        self.server.comments[0]['text']=original;self.server.comments[0]['id']='replacement'
        code,runner=self.run_gate('run','--','python','-c','x')
        self.assertEqual(code,worker_gate.ACK);runner.assert_not_called()
        self.server.comments[0]['id']='c1';self.server.comments[0]['issue_id']='wrong-task'
        code,runner=self.run_gate('run','--','python','-c','x')
        self.assertEqual(code,worker_gate.ACK);runner.assert_not_called()

    def test_duplicate_registration_and_wrong_issue_refused(self):
        main(self.argv('register'))
        self.server.comments.append(dict(self.server.comments[0]))
        self.assertEqual(main(self.argv('register')),worker_gate.ACK)
        self.server.comments.pop();self.server.issue['id']='wrong-task'
        code,runner=self.run_gate('run','--','python','-c','x')
        self.assertEqual(code,worker_gate.TRANSPORT);runner.assert_not_called()

    def test_malformed_comment_payload_fails_closed(self):
        for payload in ([],{'kind':worker_gate.KIND,'plan':[]}):
            self.assertIsNone(worker_gate.parse_body(worker_gate.PREFIX+worker_gate.canonical_bytes(payload).decode()+'\n'))

    def test_unclaimed_or_reassigned_task_means_no_execution(self):
        main(self.argv("register"))
        for status, assignee in (("open", None), ("in_progress", "someone-else"),
                                 ("closed", ACTOR), ("blocked", ACTOR)):
            self.server.issue = {"id": TASK, "status": status, "assignee": assignee}
            code, runner = self.run_gate("run", "--", "python", "-c", "x")
            self.assertEqual(code, worker_gate.CLAIM, (status, assignee))
            runner.assert_not_called()

    def test_transport_failure_on_readback_means_no_execution(self):
        main(self.argv("register"))

        def explode(config, project, actor, args):
            raise RuntimeError("SSH failed (255); outcome may be uncertain.")

        with patch.object(worker_gate, "request", side_effect=explode):
            code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, worker_gate.TRANSPORT)
        runner.assert_not_called()

    def test_tampered_receipt_means_no_execution(self):
        main(self.argv("register"))
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["plan"]["sha256"] = "1" * 64
        self.receipt.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, worker_gate.PLAN)
        runner.assert_not_called()

    def test_missing_explicit_identity_is_usage_error(self):
        for flag in ("--actor", "--cwd", "--plan", "--receipt", "--config"):
            argv = self.argv("run")
            argv[argv.index(flag) + 1] = ""
            with patch.object(worker_gate, "runner") as runner:
                self.assertEqual(main(argv), worker_gate.USAGE, flag)
                runner.assert_not_called()
        self.assertEqual(self.server.calls, [])

    # ------------------------------------------------- uncertain writes

    def test_uncertain_write_not_found_is_reconciled_once_and_never_runs(self):
        self.server.uncertain_add = "lost"
        self.assertEqual(main(self.argv("register")), worker_gate.TRANSPORT)
        self.assertEqual(len(self.server.adds()), 1)  # one attempt, no blind repeat
        self.assertEqual(self.server.comments, [])
        self.assertFalse(self.receipt.exists())
        code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, worker_gate.USAGE)  # no receipt was written
        runner.assert_not_called()

    def test_uncertain_write_that_landed_is_reconciled_by_readback(self):
        self.server.uncertain_add = "landed"
        self.assertEqual(main(self.argv("register")), 0)
        self.assertEqual(len(self.server.adds()), 1)
        self.assertTrue(self.receipt.exists())
        code, runner = self.run_gate("run", "--", "python", "-c", "x")
        self.assertEqual(code, 0)
        runner.assert_called_once()

    # ------------------------------------------------- launch ID binding

    def test_launch_id_reuse_for_different_plan_is_refused(self):
        main(self.argv("register"))
        self.plan.write_text(PLAN_TEXT + "amended\n", encoding="utf-8")
        self.receipt.unlink()
        self.assertEqual(main(self.argv("register")), worker_gate.PLAN)
        self.assertEqual(len(self.server.comments), 1)
        self.assertEqual(len(self.server.adds()), 1)

    def test_repeat_registration_does_not_rewrite(self):
        main(self.argv("register"))
        self.assertEqual(main(self.argv("register")), 0)
        self.assertEqual(len(self.server.adds()), 1)
        self.assertEqual(len(self.server.comments), 1)

    def test_run_never_claims(self):
        main(self.argv("register"))
        self.server.issue = {"id": TASK, "status": "open", "assignee": None}
        self.server.calls.clear()
        code, runner = self.run_gate("check")
        self.assertEqual(code, worker_gate.CLAIM)
        self.assertEqual([c for c in self.server.calls if c[0] == "update"], [])
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
