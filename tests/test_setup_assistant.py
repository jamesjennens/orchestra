"""Tests for the setup assistant: private config plus platform credential storage.

All file work happens in temporary directories and all credential stores are
fakes, so no real credential or installation is touched. The only test that can
reach a real platform store is the guarded smoke at the bottom, which skips
with an explicit reason unless ``ORCHESTRA_CREDENTIAL_SMOKE=1`` is set.
"""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client
import credential_store as cs
import setup_assistant as sa

#: Clearly synthetic secret values. No real credential is used in these tests.
FAKE_SECRET = "unit-test-secret-not-real"
FAKE_SECRET_OLD = "unit-test-secret-not-real-old"
SMOKE_ENV = "ORCHESTRA_CREDENTIAL_SMOKE"

ROOT = Path(__file__).resolve().parents[1]


class FakeStore(cs.CredentialStore):
    """In-memory credential store used instead of any platform store."""

    name = "fake-store"

    def __init__(self, secrets=None, *, available=True, reason="", hint=""):
        self.secrets = dict(secrets or {})
        self.state = cs.Availability(available, reason, hint)
        self.calls = []

    def availability(self):
        return self.state

    def store(self, key, secret, *, overwrite=False):
        self.require_available()
        self._guard_secret(secret)
        self._guard_overwrite(key, overwrite)
        self.calls.append(("store", key))
        self.secrets[key] = secret

    def retrieve(self, key):
        self.require_available()
        return self.secrets.get(key)

    def delete(self, key):
        self.require_available()
        self.calls.append(("delete", key))
        return self.secrets.pop(key, None) is not None

    def exists(self, key):
        self.require_available()
        return key in self.secrets


class SetupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "client.local.json"
        self.store = FakeStore()
        self.logs = []

    def request(self, **overrides):
        values = dict(
            project="example",
            config_path=str(self.config),
            transport="ssh",
            host="example-host",
            endpoint="/srv/orchestra/endpoint.py",
            root="/srv/orchestra/runtime",
            secret=FAKE_SECRET,
        )
        values.update(overrides)
        return sa.SetupRequest(**values)

    def run_setup(self, *, confirm_config=False, confirm_credential=False, **overrides):
        return sa.run_setup(self.request(**overrides), store=self.store, interactive=False,
                            confirm_config=confirm_config, confirm_credential=confirm_credential,
                            log=self.logs.append)

    def call_main(self, *argv, store=None, stdin_text="", log=None):
        out, err = io.StringIO(), io.StringIO()
        code = sa.main(list(argv), store=store if store is not None else self.store, log=log,
                       stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def config_dict(self):
        return json.loads(self.config.read_text(encoding="utf-8"))


class FreshSetupTests(SetupTestCase):
    def test_writes_a_private_config_and_stores_the_secret(self):
        result = self.run_setup()
        self.assertTrue(result.config_written)
        self.assertTrue(result.secret_stored)
        self.assertTrue(self.config.exists())
        config = self.config_dict()
        self.assertEqual(config["transport"], "ssh")
        self.assertEqual(config["host"], "example-host")
        self.assertEqual(config["endpoint"], "/srv/orchestra/endpoint.py")
        self.assertEqual(config["project"], "example")
        self.assertEqual(config["credential"], {"store": "fake-store", "service": "orchestra", "key": "example"})
        self.assertEqual(self.store.secrets["example"], FAKE_SECRET)

    def test_config_file_never_contains_the_secret(self):
        self.run_setup()
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn(FAKE_SECRET, text)
        self.assertNotIn(FAKE_SECRET, json.dumps(self.config_dict()))

    @unittest.skipUnless(os.name == "posix", "owner-only file mode is a POSIX property")
    def test_config_file_is_owner_only(self):
        self.run_setup()
        mode = stat.S_IMODE(self.config.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_private_directory_is_created_for_a_new_config_path(self):
        nested = self.dir / "private" / "client.local.json"
        result = self.run_setup(config_path=str(nested))
        self.assertTrue(nested.exists())
        self.assertEqual(result.config_path, str(nested))

    def test_local_transport_defaults_the_python_interpreter(self):
        result = self.run_setup(transport="local", host=None, endpoint="/srv/endpoint.py",
                                root="/srv/runtime")
        self.assertEqual(result.config["python"], "python3")
        self.assertEqual(result.config["root"], "/srv/runtime")

    def test_no_secret_setup_writes_no_credential_block(self):
        result = self.run_setup(store_secret=False, secret=None)
        self.assertFalse(result.secret_stored)
        self.assertNotIn("credential", self.config_dict())
        self.assertEqual(self.store.calls, [])

    def test_services_are_generic_config_data(self):
        services = (sa.ServiceSpec.parse("reports=https://reports.example.test"),
                    sa.ServiceSpec.parse("calendar=https://calendar.example.test|read only"))
        result = self.run_setup(services=services)
        self.assertEqual(result.config["services"], [
            {"name": "reports", "address": "https://reports.example.test"},
            {"name": "calendar", "address": "https://calendar.example.test", "notes": "read only"},
        ])
        self.assertNotIn("jjbp", json.dumps(result.config).lower())

    def test_malformed_service_spec_is_a_usage_error(self):
        for value in ("no-separator", "=address", "name="):
            with self.assertRaises(sa.SetupError) as caught:
                sa.ServiceSpec.parse(value)
            self.assertEqual(caught.exception.exit_code, sa.EXIT_USAGE)

    def test_dry_run_writes_nothing_and_stores_nothing(self):
        result = sa.run_setup(self.request(secret=None), store=self.store, interactive=False,
                              dry_run=True, log=self.logs.append)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.config_written)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.store.secrets, {})


class RepeatSetupTests(SetupTestCase):
    def test_existing_config_is_not_overwritten_without_confirmation(self):
        self.config.write_text('{"keep": "original"}', encoding="utf-8")
        with self.assertRaises(sa.SetupError) as caught:
            self.run_setup()
        self.assertEqual(caught.exception.exit_code, sa.EXIT_CONFIG_EXISTS)
        self.assertIn("--force-config", str(caught.exception))
        self.assertEqual(self.config.read_text(encoding="utf-8"), '{"keep": "original"}')
        self.assertEqual(self.store.secrets, {})

    def test_existing_config_is_replaced_with_explicit_confirmation(self):
        self.config.write_text('{"keep": "original"}', encoding="utf-8")
        result = self.run_setup(confirm_config=True)
        self.assertTrue(result.replaced_config)
        self.assertEqual(self.config_dict()["host"], "example-host")
        self.assertEqual(self.store.secrets["example"], FAKE_SECRET)

    def test_existing_credential_is_not_replaced_without_confirmation(self):
        self.store = FakeStore({"example": FAKE_SECRET_OLD})
        with self.assertRaises(sa.SetupError) as caught:
            self.run_setup()
        self.assertEqual(caught.exception.exit_code, sa.EXIT_CREDENTIAL_EXISTS)
        self.assertIn("--force-credential", str(caught.exception))
        self.assertEqual(self.store.secrets["example"], FAKE_SECRET_OLD)
        self.assertFalse(self.config.exists())

    def test_existing_credential_is_replaced_with_explicit_confirmation(self):
        self.store = FakeStore({"example": FAKE_SECRET_OLD})
        result = self.run_setup(confirm_credential=True)
        self.assertTrue(result.replaced_credential)
        self.assertEqual(self.store.secrets["example"], FAKE_SECRET)
        self.assertTrue(self.config.exists())

    def test_interactive_confirmation_accepts_and_declines(self):
        self.config.write_text('{"keep": "original"}', encoding="utf-8")
        answers = iter(["n"])
        with self.assertRaises(sa.SetupError):
            sa.run_setup(self.request(), store=self.store, interactive=True,
                         input_fn=lambda prompt: next(answers), log=self.logs.append)
        answers = iter(["y"])
        result = sa.run_setup(self.request(), store=self.store, interactive=True,
                              input_fn=lambda prompt: next(answers), log=self.logs.append)
        self.assertTrue(result.replaced_config)


class MissingStoreTests(SetupTestCase):
    def unavailable_store(self):
        return FakeStore(available=False, reason="the 'secret-tool' command was not found on PATH",
                         hint="install libsecret-tools; no plaintext fallback is used")

    def test_unavailable_store_fails_before_anything_is_written(self):
        self.store = self.unavailable_store()
        with self.assertRaises(cs.CredentialStoreUnavailable) as caught:
            self.run_setup()
        message = str(caught.exception)
        self.assertIn("secret-tool", message)
        self.assertIn("install libsecret-tools", message)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.store.calls, [])

    def test_cli_reports_an_unavailable_store_with_its_own_exit_code(self):
        code, out, err = self.call_main("--project", "example", "--config", str(self.config),
                                        "--host", "example-host", "--endpoint", "/srv/endpoint.py",
                                        "--root", "/srv/orchestra/runtime",
                                        "--secret-stdin", "--non-interactive",
                                        stdin_text=FAKE_SECRET + "\n",
                                        store=self.unavailable_store())
        self.assertEqual(code, sa.EXIT_STORE_UNAVAILABLE)
        self.assertIn("secret-tool", err)
        self.assertFalse(self.config.exists())

    def test_credential_write_failure_is_reported_and_rolled_back(self):
        class FailingStore(FakeStore):
            def store(self, key, secret, *, overwrite=False):
                raise cs.CredentialStoreError("fake-store: the platform store refused the write")

        self.store = FailingStore()
        with self.assertRaises(cs.CredentialStoreError):
            self.run_setup()
        self.assertFalse(self.config.exists())

    def test_config_write_failure_removes_the_new_credential(self):
        with patch.object(sa, "write_private_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_setup()
        self.assertFalse(self.config.exists())
        self.assertEqual(self.store.secrets, {})


class NonInteractiveTests(SetupTestCase):
    def test_missing_required_options_fail_cleanly_without_a_traceback(self):
        code, _, err = self.call_main("--project", "example", "--config", str(self.config),
                                      "--endpoint", "/srv/endpoint.py", "--secret-stdin",
                                      "--non-interactive", stdin_text=FAKE_SECRET + "\n")
        self.assertEqual(code, sa.EXIT_USAGE)
        self.assertIn("--host", err)
        self.assertIn("--root", err)
        self.assertIn("missing required input", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.config.exists())

    def test_ssh_config_requires_a_root_because_the_client_does(self):
        with self.assertRaises(sa.SetupError) as caught:
            sa.run_setup(self.request(root=None), store=self.store, interactive=False)
        self.assertIn("--root", str(caught.exception))
        self.assertFalse(self.config.exists())

    def test_missing_secret_fails_cleanly(self):
        with self.assertRaises(sa.SetupError) as caught:
            sa.run_setup(self.request(secret=None), store=self.store, interactive=False,
                         secret_reader=None, log=self.logs.append)
        self.assertEqual(caught.exception.exit_code, sa.EXIT_USAGE)
        self.assertIn("--secret-stdin", str(caught.exception))
        self.assertFalse(self.config.exists())
        self.assertEqual(self.store.secrets, {})

    def test_empty_stdin_secret_fails_cleanly(self):
        code, _, err = self.call_main("--project", "example", "--config", str(self.config),
                                      "--host", "example-host", "--endpoint", "/srv/endpoint.py",
                                      "--secret-stdin", "--non-interactive", stdin_text="")
        self.assertEqual(code, sa.EXIT_USAGE)
        self.assertIn("--secret-stdin", err)
        self.assertFalse(self.config.exists())

    def test_bad_transport_is_rejected(self):
        with self.assertRaises(sa.SetupError) as caught:
            sa.run_setup(self.request(transport="telnet"), store=self.store, interactive=False)
        self.assertEqual(caught.exception.exit_code, sa.EXIT_USAGE)
        self.assertIn("--transport", str(caught.exception))

    def test_transports_match_the_client_contract(self):
        self.assertEqual(set(sa.TRANSPORTS), client.TRANSPORTS)


class RedactionTests(SetupTestCase):
    def test_secret_never_appears_in_output_or_config(self):
        code, out, err = self.call_cli_with_secret()
        self.assertEqual(code, sa.EXIT_OK)
        for text in (out, err, self.config.read_text(encoding="utf-8")):
            self.assertNotIn(FAKE_SECRET, text)

    def call_cli_with_secret(self, log=None, config_path=None):
        out, err = io.StringIO(), io.StringIO()
        code = sa.main(
            ["--project", "example", "--config", str(config_path or self.config),
             "--host", "example-host", "--endpoint", "/srv/endpoint.py",
             "--root", "/srv/orchestra/runtime",
             "--secret-stdin", "--non-interactive", "--json"],
            store=self.store, log=log, stdin=io.StringIO(FAKE_SECRET + "\n"),
            stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_log_lines_are_redacted(self):
        code, _, _ = self.call_cli_with_secret(log=self.logs.append,
                                               config_path=self.dir / "logged.json")
        self.assertEqual(code, sa.EXIT_OK)
        self.assertTrue(self.logs)
        self.assertTrue(any("stored credential" in line for line in self.logs))
        for line in self.logs:
            self.assertNotIn(FAKE_SECRET, line)

    def test_result_json_is_secret_free_and_reports_the_store(self):
        code, out, _ = self.call_cli_with_secret()
        self.assertEqual(code, sa.EXIT_OK)
        payload = json.loads(out[out.index("{"):])
        self.assertEqual(payload["credential"]["key"], "example")
        self.assertEqual(payload["credential"]["store"]["backend"], "fake-store")
        self.assertTrue(payload["credential"]["stored"])
        self.assertNotIn(FAKE_SECRET, json.dumps(payload))

    def test_run_setup_log_lines_are_redacted(self):
        sa.run_setup(self.request(), store=self.store, interactive=False, log=self.logs.append)
        self.assertTrue(any("wrote private config" in line for line in self.logs))
        for line in self.logs:
            self.assertNotIn(FAKE_SECRET, line)

    def test_request_repr_hides_the_secret(self):
        self.assertNotIn(FAKE_SECRET, repr(self.request()))

    def test_interactive_prompt_uses_getpass_and_shows_no_secret(self):
        prompts = []

        def fake_getpass(prompt):
            prompts.append(prompt)
            return FAKE_SECRET

        answers = iter(["prompted-host", "/srv/prompted/endpoint.py"])
        result = sa.run_setup(self.request(host=None, endpoint=None, secret=None), store=self.store,
                              interactive=True, input_fn=lambda prompt: next(answers),
                              getpass_fn=fake_getpass, log=self.logs.append)
        self.assertTrue(result.secret_stored)
        self.assertEqual(self.config_dict()["host"], "prompted-host")
        self.assertEqual(len(prompts), 1)
        self.assertNotIn(FAKE_SECRET, prompts[0])
        for line in self.logs:
            self.assertNotIn(FAKE_SECRET, line)

    def test_cli_dry_run_prints_a_secret_free_config(self):
        out, err = io.StringIO(), io.StringIO()
        code = sa.main(["--project", "example", "--config", str(self.config), "--host", "example-host",
                        "--endpoint", "/srv/endpoint.py", "--root", "/srv/orchestra/runtime",
                        "--dry-run", "--non-interactive"],
                       store=self.store, stdin=io.StringIO(""), stdout=out, stderr=err)
        self.assertEqual(code, sa.EXIT_OK)
        printed = out.getvalue()
        self.assertIn('"transport": "ssh"', printed)
        self.assertNotIn(FAKE_SECRET, printed)
        self.assertFalse(self.config.exists())


class GenericRepositoryTests(unittest.TestCase):
    def test_new_modules_contain_no_project_specific_defaults(self):
        for module in (sa, cs):
            source = Path(module.__file__).read_text(encoding="utf-8").lower()
            self.assertNotIn("jjbp", source, f"{module.__name__} must stay project-generic")

    def test_config_keys_are_the_ones_the_client_reads(self):
        request = sa.SetupRequest(project="example", config_path="config.json", transport="ssh",
                                  host="example-host", endpoint="/srv/endpoint.py",
                                  root="/srv/orchestra/runtime")
        config = sa.build_config(request)
        self.assertNotIn("secret", json.dumps(config).lower())
        argv, label = client._argv(config)
        self.assertEqual(label, "SSH")
        self.assertIn("example-host", argv)
        # The client requires the root path, which is why setup requires it too.
        with self.assertRaises(ValueError):
            client._argv(sa.build_config(sa.SetupRequest(host="example-host",
                                                         endpoint="/srv/endpoint.py")))


class PlatformSmokeTests(unittest.TestCase):
    """Guarded smoke against a real platform store: opt in, then clean up."""

    def test_platform_store_round_trip_and_cleanup(self):
        if os.environ.get(SMOKE_ENV) != "1":
            self.skipTest(
                "platform credential-store smoke not requested; set "
                f"{SMOKE_ENV}=1 to store, retrieve and delete a clearly-labelled TEST "
                f"credential in the real {sys.platform} store")
        try:
            store = cs.default_store()
        except cs.CredentialStoreUnavailable as exc:
            self.skipTest(f"no credential-store backend for platform {sys.platform!r}: {exc}")
        state = store.availability()
        if not state.ok:
            self.skipTest(f"real {store.name} store unavailable here: {state.reason} ({state.hint})")

        key = "orchestra-smoke-" + uuid.uuid4().hex
        secret = "TEST-NOT-A-REAL-SECRET-" + uuid.uuid4().hex
        try:
            store.store(key, secret)
            self.assertTrue(store.exists(key), "the smoke credential must be readable")
            self.assertEqual(secret, store.retrieve(key), "retrieved secret must equal the stored one")
        finally:
            try:
                store.delete(key)
            finally:
                leftover = True
                try:
                    leftover = store.exists(key)
                except Exception:
                    leftover = True
                self.assertFalse(leftover, f"smoke credential {key!r} must not survive the test")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
