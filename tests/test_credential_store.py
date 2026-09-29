"""Unit tests for the platform credential-store adapters.

No real credentials are used anywhere in this file: every value is a clearly
fake, synthetic string, and the platform command-line tools are replaced by
recording fakes. The one test that can touch a real platform store is the
guarded smoke in ``tests/test_setup_assistant.py``.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import credential_store as cs

FAKE_SECRET = "unit-test-secret-not-real"
FAKE_SECRET_LONG = "unit-test-secret-not-real-longer"


class FakeWinError(Exception):
    """Stand-in for ``pywintypes.error`` (which carries ``winerror``)."""

    def __init__(self, winerror):
        super().__init__(f"Windows error {winerror}")
        self.winerror = winerror


class FakeRunner:
    """Records argv/stdin and returns scripted ``CompletedProcess`` results."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, argv, input=None, capture_output=True, text=True, check=False):
        self.calls.append({"argv": list(argv), "input": input})
        returncode, stdout, stderr = self.results.pop(0) if self.results else (0, "", "")
        return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)

    def argv_text(self):
        return [" ".join(call["argv"]) for call in self.calls]


class FakeWin32Cred:
    """Minimal win32cred stand-in that mimics the real UTF-16-LE blob."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, items=None, blob_type=bytes):
        self.items = dict(items or {})
        self.blob_type = blob_type
        self.writes = []
        self.deletes = []

    def CredWrite(self, credential, flags):
        self.writes.append((dict(credential), flags))
        blob = credential["CredentialBlob"].encode("utf-16-le")
        self.items[credential["TargetName"]] = blob

    def CredRead(self, target, kind):
        if target not in self.items:
            raise FakeWinError(1168)
        blob = self.items[target]
        if self.blob_type is str and isinstance(blob, bytes):
            blob = blob.decode("utf-16-le").rstrip("\x00")
        return {"CredentialBlob": blob, "TargetName": target, "UserName": "orchestra"}

    def CredDelete(self, target, kind, flags):
        if target not in self.items:
            raise FakeWinError(1168)
        self.deletes.append(target)
        del self.items[target]
        return True


class RecordingStore(cs.CredentialStore):
    """In-memory backend used to exercise the shared interface behaviour."""

    name = "recording-store"

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
        self.calls.append(("retrieve", key))
        return self.secrets.get(key)

    def delete(self, key):
        self.require_available()
        self.calls.append(("delete", key))
        return self.secrets.pop(key, None) is not None

    def exists(self, key):
        self.require_available()
        return key in self.secrets


class RedactionTests(unittest.TestCase):
    def test_replaces_every_occurrence_longest_first(self):
        text = f"secret={FAKE_SECRET} longer={FAKE_SECRET_LONG}"
        redacted = cs.redact_secrets(text, [FAKE_SECRET, FAKE_SECRET_LONG])
        self.assertEqual(redacted, f"secret={cs.REDACTED} longer={cs.REDACTED}")
        self.assertNotIn(FAKE_SECRET, redacted)

    def test_ignores_empty_and_missing_values(self):
        text = "no secrets here"
        self.assertEqual(cs.redact_secrets(text, [None, ""]), text)
        self.assertEqual(cs.redact_secrets(None, [FAKE_SECRET]), "")


class InterfaceTests(unittest.TestCase):
    def test_unavailable_store_refuses_every_operation(self):
        store = RecordingStore(available=False, reason="the 'secret-tool' command was not found on PATH",
                               hint="install libsecret-tools")
        self.assertFalse(store.available())
        for call in (lambda: store.store("key", FAKE_SECRET),
                     lambda: store.retrieve("key"),
                     lambda: store.delete("key"),
                     lambda: store.exists("key")):
            with self.assertRaises(cs.CredentialStoreUnavailable) as caught:
                call()
            message = str(caught.exception)
            self.assertIn("secret-tool", message)
            self.assertIn("libsecret-tools", message)
            self.assertNotIn(FAKE_SECRET, message)

    def test_describe_reports_state_without_secrets(self):
        store = RecordingStore({"key": FAKE_SECRET})
        described = store.describe()
        self.assertEqual(described, {"backend": "recording-store", "available": True})
        self.assertNotIn(FAKE_SECRET, repr(described))
        broken = RecordingStore(available=False, reason="missing tool", hint="install it")
        self.assertEqual(broken.describe()["reason"], "missing tool")
        self.assertFalse(broken.describe()["available"])

    def test_existing_credential_is_not_replaced_silently(self):
        store = RecordingStore({"key": "original-value"})
        with self.assertRaises(cs.CredentialExistsError) as caught:
            store.store("key", FAKE_SECRET)
        self.assertIn("--force-credential", str(caught.exception))
        self.assertEqual(store.secrets["key"], "original-value")
        store.store("key", FAKE_SECRET, overwrite=True)
        self.assertEqual(store.secrets["key"], FAKE_SECRET)

    def test_empty_secret_is_rejected(self):
        store = RecordingStore()
        for value in ("", None):
            with self.assertRaises(cs.CredentialStoreError):
                store.store("key", value)


class LinuxSecretServiceTests(unittest.TestCase):
    def build(self, results):
        runner = FakeRunner(results)
        store = cs.LinuxSecretServiceStore(runner=runner, which=lambda name: "/usr/bin/" + name)
        return store, runner

    def test_store_sends_the_secret_on_stdin_and_never_in_argv(self):
        store, runner = self.build([(0, "", "")])
        with patch.object(sys, "platform", "linux"):
            store.store("example", FAKE_SECRET, overwrite=True)
        call = runner.calls[0]
        self.assertEqual(call["input"], FAKE_SECRET + "\n")
        self.assertNotIn(FAKE_SECRET, " ".join(call["argv"]))
        self.assertEqual(call["argv"][1], "store")
        self.assertIn("service", call["argv"])
        self.assertIn("example", call["argv"])

    def test_retrieve_returns_the_value_and_strips_one_newline(self):
        store, runner = self.build([(0, FAKE_SECRET + "\n", ""), (0, FAKE_SECRET + "\n", "")])
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(store.retrieve("example"), FAKE_SECRET)
            self.assertTrue(store.exists("example"))
        self.assertEqual([call["argv"][1] for call in runner.calls], ["lookup", "lookup"])

    def test_absent_item_is_none_not_an_error(self):
        # Real `secret-tool lookup` for a missing item: exit 1, no stdout, no stderr.
        store, _ = self.build([(1, "", "")])
        with patch.object(sys, "platform", "linux"):
            self.assertIsNone(store.retrieve("example"))
            self.assertFalse(store.exists("example"))

    def test_absent_item_with_a_not_found_message_is_also_none(self):
        store, _ = self.build([(1, "", "No such secret item at path: /org/freedesktop/secrets/collection/x")])
        with patch.object(sys, "platform", "linux"):
            self.assertIsNone(store.retrieve("example"))

    def test_lookup_failure_with_output_is_still_a_real_error(self):
        # Only a *silent* exit 1 means "absent"; anything it says is a failure.
        store, _ = self.build([(1, "", "Cannot autolaunch D-Bus without X11 $DISPLAY")])
        with patch.object(sys, "platform", "linux"):
            with self.assertRaises(cs.CredentialStoreError):
                store.retrieve("example")

    def test_retrieve_strips_exactly_one_trailing_newline(self):
        store, _ = self.build([(0, FAKE_SECRET + "\n", ""), (0, "line-one\nline-two\n", "")])
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(store.retrieve("example"), FAKE_SECRET)
            # Only the tool's terminating newline is removed; an embedded one stays.
            self.assertEqual(store.retrieve("example"), "line-one\nline-two")

    def test_store_failure_raises_redacted_detail(self):
        store, _ = self.build([(1, "", f"Cannot reach the Secret Service; input was {FAKE_SECRET}")])
        with patch.object(sys, "platform", "linux"):
            with self.assertRaises(cs.CredentialStoreError) as caught:
                store.store("example", FAKE_SECRET, overwrite=True)
        message = str(caught.exception)
        self.assertIn("Cannot reach the Secret Service", message)
        self.assertNotIn(FAKE_SECRET, message)

    def test_unreachable_store_is_a_real_error_not_absent(self):
        store, _ = self.build([(2, "", "Cannot autolaunch D-Bus without X11 $DISPLAY")])
        with patch.object(sys, "platform", "linux"):
            with self.assertRaises(cs.CredentialStoreError) as caught:
                store.retrieve("example")
        self.assertIn("D-Bus", str(caught.exception))

    def test_delete_is_honest_about_present_and_absent_items(self):
        store, runner = self.build([
            (0, FAKE_SECRET + "\n", ""),  # lookup precheck: present
            (0, "", ""),                  # clear: succeeded
            (1, "", ""),                  # lookup precheck: absent (real secret-tool)
        ])
        with patch.object(sys, "platform", "linux"):
            self.assertTrue(store.delete("example"))
            self.assertFalse(store.delete("example"))
        self.assertEqual([call["argv"][1] for call in runner.calls], ["lookup", "clear", "lookup"])

    def test_clear_exit_zero_without_a_match_is_not_reported_as_deleted(self):
        # `secret-tool clear` exits 0 even when nothing matched; the absent
        # precheck means clear is never run and the result is honestly False.
        store, runner = self.build([(1, "", "")])
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(store.delete("example"))
        self.assertEqual([call["argv"][1] for call in runner.calls], ["lookup"])

    def test_delete_failure_is_a_real_error(self):
        store, _ = self.build([(0, FAKE_SECRET + "\n", ""), (2, "", "D-Bus is not running")])
        with patch.object(sys, "platform", "linux"):
            with self.assertRaises(cs.CredentialStoreError):
                store.delete("example")

    def test_missing_secret_tool_is_actionable_and_never_falls_back(self):
        store = cs.LinuxSecretServiceStore(runner=FakeRunner(), which=lambda name: None)
        with patch.object(sys, "platform", "linux"):
            state = store.availability()
            self.assertFalse(state.ok)
            self.assertIn("secret-tool", state.reason)
            self.assertIn("libsecret-tools", state.hint)
            with self.assertRaises(cs.CredentialStoreUnavailable):
                store.store("example", FAKE_SECRET)

    def test_other_platforms_report_their_platform(self):
        store = cs.LinuxSecretServiceStore(runner=FakeRunner(), which=lambda name: "/usr/bin/" + name)
        with patch.object(sys, "platform", "win32"):
            self.assertIn("win32", store.availability().reason)


class MacKeychainTests(unittest.TestCase):
    def build(self, results):
        runner = FakeRunner(results)
        return cs.MacKeychainStore(runner=runner, which=lambda name: "/usr/bin/" + name), runner

    def test_store_uses_security_interactive_and_keeps_the_secret_out_of_argv(self):
        # `security add-generic-password ... -w` with no value prompts on
        # /dev/tty; the secret is fed to `security -i` on stdin instead.
        store, runner = self.build([(0, "", "")])
        with patch.object(sys, "platform", "darwin"):
            store.store("example", FAKE_SECRET, overwrite=True)
        call = runner.calls[0]
        self.assertEqual(call["argv"], ["/usr/bin/security", "-i"])
        self.assertNotIn(FAKE_SECRET, " ".join(call["argv"]))
        self.assertIn("add-generic-password", call["input"])
        self.assertIn(FAKE_SECRET, call["input"])

    def test_store_failure_is_redacted(self):
        store, _ = self.build([(1, "", f"security: failed while handling {FAKE_SECRET}")])
        with patch.object(sys, "platform", "darwin"):
            with self.assertRaises(cs.CredentialStoreError) as caught:
                store.store("example", FAKE_SECRET, overwrite=True)
        self.assertNotIn(FAKE_SECRET, str(caught.exception))

    def test_retrieve_and_delete(self):
        store, _ = self.build([(0, FAKE_SECRET + "\n", ""), (44, "", "could not be found in the keychain")])
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(store.retrieve("example"), FAKE_SECRET)
            self.assertFalse(store.delete("example"))

    def test_unavailable_off_macos(self):
        store, _ = self.build([])
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(store.availability().ok)
        store = cs.MacKeychainStore(runner=FakeRunner(), which=lambda name: None)
        with patch.object(sys, "platform", "darwin"):
            self.assertIn("security", store.availability().reason)


class WindowsCredentialManagerTests(unittest.TestCase):
    def build(self, module=None):
        module = module if module is not None else FakeWin32Cred()
        return cs.WindowsCredentialStore(module=module), module

    def test_round_trip_decodes_the_utf16le_blob(self):
        store, module = self.build()
        secret = "smoke-\u00e9\u4e2d\u6587-value"
        store.store("example", secret)
        self.assertEqual(store.retrieve("example"), secret)
        self.assertTrue(store.exists("example"))
        self.assertTrue(store.delete("example"))
        self.assertFalse(store.exists("example"))
        self.assertEqual(module.writes[0][0]["TargetName"], "orchestra:example")

    def test_str_blobs_are_supported(self):
        module = FakeWin32Cred(blob_type=str)
        store = cs.WindowsCredentialStore(module=module)
        store.store("example", FAKE_SECRET)
        self.assertEqual(store.retrieve("example"), FAKE_SECRET)

    def test_target_name_is_namespaced(self):
        store = cs.WindowsCredentialStore(service="team", module=FakeWin32Cred())
        self.assertEqual(store.target_name("demo"), "team:demo")

    def test_existing_credential_is_not_replaced_silently(self):
        store, module = self.build()
        store.store("example", "original-value")
        with self.assertRaises(cs.CredentialExistsError):
            store.store("example", FAKE_SECRET)
        self.assertEqual(store.retrieve("example"), "original-value")
        store.store("example", FAKE_SECRET, overwrite=True)
        self.assertEqual(store.retrieve("example"), FAKE_SECRET)

    def test_missing_delete_is_false_not_an_error(self):
        store, _ = self.build()
        self.assertFalse(store.delete("absent"))

    def test_write_failure_is_wrapped(self):
        class Broken(FakeWin32Cred):
            def CredWrite(self, credential, flags):
                raise RuntimeError("Credential Manager refused the write")

        store = cs.WindowsCredentialStore(module=Broken())
        with self.assertRaises(cs.CredentialStoreError) as caught:
            store.store("example", FAKE_SECRET)
        self.assertIn("Credential Manager", str(caught.exception))

    def test_missing_pywin32_is_actionable_and_never_falls_back(self):
        store = cs.WindowsCredentialStore()
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"win32cred": None}):
            state = store.availability()
            self.assertFalse(state.ok)
            self.assertIn("pywin32", state.reason)
            self.assertIn("pip install pywin32", state.hint)
            with self.assertRaises(cs.CredentialStoreUnavailable):
                store.store("example", FAKE_SECRET)

    def test_unavailable_off_windows(self):
        store = cs.WindowsCredentialStore()
        with patch.object(sys, "platform", "linux"):
            self.assertIn("linux", store.availability().reason)


class FactoryTests(unittest.TestCase):
    def test_platform_selection(self):
        self.assertIsInstance(cs.default_store("win32"), cs.WindowsCredentialStore)
        self.assertIsInstance(cs.default_store("darwin"), cs.MacKeychainStore)
        self.assertIsInstance(cs.default_store("linux"), cs.LinuxSecretServiceStore)
        self.assertIsInstance(cs.default_store("linux2"), cs.LinuxSecretServiceStore)

    def test_unsupported_platform_is_an_error_not_a_fallback(self):
        with self.assertRaises(cs.CredentialStoreUnavailable) as caught:
            cs.default_store("aix")
        self.assertIn("aix", str(caught.exception))

    def test_service_namespace_is_passed_through(self):
        store = cs.default_store("win32", service="team")
        self.assertEqual(store.service, "team")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
