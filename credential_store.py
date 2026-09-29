"""Platform credential-store adapters for project setup.

Orchestra keeps secrets out of the repository and out of its own configuration
files: setup writes a private, non-secret config and asks the operating
system's credential store to hold any secret. This module is that layer.

Interface
---------
``CredentialStore`` exposes five operations:

* ``availability()`` -> ``Availability`` -- is the platform store usable here,
  and if not, why and what to do about it;
* ``store(key, secret, overwrite=False)``;
* ``retrieve(key)`` -> ``str | None``;
* ``delete(key)`` -> ``bool`` (``False`` when nothing was stored);
* ``exists(key)`` -> ``bool``.

Backends: ``WindowsCredentialStore`` (Credential Manager through pywin32
``win32cred``), ``MacKeychainStore`` (the ``security`` CLI) and
``LinuxSecretServiceStore`` (Secret Service through ``secret-tool``); use
``default_store()`` to pick the one for the current platform. Each backend
raises ``CredentialStoreUnavailable`` with an actionable message when its store
cannot be reached. No backend falls back to a plaintext file: an unavailable
store is an error, never a downgrade.

The macOS backend feeds ``security -i`` (its stdin command mode) rather than
``security add-generic-password ... -w`` without a value: the latter prompts on
``/dev/tty`` and ignores piped stdin. The Linux backend knows that
``secret-tool lookup`` exits 1 with empty output for a missing item and that
``secret-tool clear`` exits 0 even when nothing matched.

Secret handling
---------------
Secrets are passed to helper processes on **stdin**, never in ``argv`` (argv is
readable by other users through the process list on many systems), are never
included in log or error text, and ``redact_secrets()`` masks them in any text
that is about to be displayed or logged.

Only the standard library is required. pywin32 is used on Windows only, and
only when the Windows backend is actually exercised.
"""
from __future__ import annotations

import abc
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

DEFAULT_SERVICE = "orchestra"
REDACTED = "***REDACTED***"
DETAIL_LIMIT = 300

#: Windows ERROR_NOT_FOUND, returned by CredRead/CredDelete for a missing entry.
_WINDOWS_NOT_FOUND = 1168

#: English text the platform tools print for an absent item. Anything else is a
#: real failure and is raised rather than reported as "absent".
_NOT_FOUND_MARKERS = ("no such secret", "no such item", "not be found", "not found", "no such file")


class CredentialStoreError(Exception):
    """A credential-store operation failed."""


class CredentialStoreUnavailable(CredentialStoreError):
    """The platform store cannot be used here; the message says what to do."""


class CredentialExistsError(CredentialStoreError):
    """A credential already exists and overwriting it was not confirmed."""


@dataclass(frozen=True)
class Availability:
    """Whether a store is usable, and if not, why and how to fix it."""

    ok: bool
    reason: str = ""
    hint: str = ""


def redact_secrets(text: object, secrets: Iterable[Optional[str]]) -> str:
    """Return ``text`` with every non-empty secret replaced by ``REDACTED``.

    Longer secrets are replaced first so that a secret which contains another
    one cannot leave a fragment behind.
    """
    result = "" if text is None else str(text)
    ordered = sorted({s for s in secrets if s}, key=len, reverse=True)
    for secret in ordered:
        result = result.replace(secret, REDACTED)
    return result


def _clean_detail(completed: "subprocess.CompletedProcess", secrets: Sequence[Optional[str]] = ()) -> str:
    """A short, redacted, single-line description of a failed helper command."""
    raw = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    text = " ".join(redact_secrets(raw, secrets).split())
    return text[:DETAIL_LIMIT]


def _is_not_found(detail: str) -> bool:
    """Whether tool output describes an absent item rather than a broken store."""
    lowered = detail.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


class CredentialStore(abc.ABC):
    """Small platform credential-store interface.

    Keys are logical names (for example ``"example-project"``); each backend
    maps them onto its own namespace. Implementations must raise
    ``CredentialStoreUnavailable`` when the platform store cannot be reached
    and must never persist a secret to a plaintext file as a fallback.
    """

    #: Short, stable backend name used in config and log output.
    name = "credential-store"

    def availability(self) -> Availability:
        """Whether this backend can be used in the current environment."""
        return Availability(True)

    def available(self) -> bool:
        return self.availability().ok

    def require_available(self) -> None:
        state = self.availability()
        if not state.ok:
            detail = state.reason or "the platform credential store is not usable here"
            hint = " " + state.hint if state.hint else ""
            raise CredentialStoreUnavailable(f"{self.name}: {detail}.{hint}")

    def describe(self) -> dict:
        """A JSON-serialisable, secret-free description for setup output."""
        state = self.availability()
        described = {"backend": self.name, "available": state.ok}
        if not state.ok:
            described["reason"] = state.reason
            described["hint"] = state.hint
        return described

    def _guard_overwrite(self, key: str, overwrite: bool) -> None:
        """Refuse to replace a stored credential without explicit confirmation."""
        if not overwrite and self.exists(key):
            raise CredentialExistsError(
                f"{self.name}: a credential already exists for {key!r}; "
                "existing credentials are never replaced silently "
                "(pass overwrite=True, or --force-credential on the setup CLI)"
            )

    @staticmethod
    def _guard_secret(secret: object) -> None:
        if not isinstance(secret, str) or not secret:
            raise CredentialStoreError("secret must be a non-empty string")

    @abc.abstractmethod
    def store(self, key: str, secret: str, *, overwrite: bool = False) -> None:
        """Store ``secret`` under ``key``, refusing silent replacement."""

    @abc.abstractmethod
    def retrieve(self, key: str) -> Optional[str]:
        """Return the stored secret, or ``None`` when ``key`` is absent."""

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """Remove ``key``; return ``True`` when something was removed."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """Whether ``key`` currently has a stored secret."""


def _default_runner(argv: List[str], **kwargs) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, **kwargs)


class _CommandStore(CredentialStore):
    """Shared plumbing for backends driven by a platform command-line tool."""

    executable = ""

    #: Exit codes that mean "the item is absent" when the command prints
    #: nothing at all. ``secret-tool lookup`` exits 1 with empty output for a
    #: missing item; treating that as anything but "absent" would make a fresh
    #: Linux setup fail.
    silent_miss_exit_codes: Tuple[int, ...] = ()

    def __init__(
        self,
        service: str = DEFAULT_SERVICE,
        *,
        runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.service = service
        self._runner = runner or _default_runner
        self._which = which or shutil.which

    def _command_path(self) -> Optional[str]:
        return self._which(self.executable)

    def _run_raw(
        self, argv: Sequence[str], *, stdin_text: Optional[str] = None
    ) -> "subprocess.CompletedProcess":
        command = list(argv)
        try:
            return self._runner(command, input=stdin_text, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:  # pragma: no cover - availability() normally catches this
            raise CredentialStoreUnavailable(
                f"{self.name}: the {self.executable!r} command was not found on PATH "
                f"({exc}); install it or use another credential store"
            ) from exc
        except OSError as exc:
            raise CredentialStoreError(
                f"{self.name}: could not run {self.executable!r}: {exc}"
            ) from exc

    def _run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: Optional[str] = None,
        secrets: Sequence[Optional[str]] = (),
        action: str = "run",
    ) -> "subprocess.CompletedProcess":
        completed = self._run_raw(argv, stdin_text=stdin_text)
        if completed.returncode != 0:
            detail = _clean_detail(completed, secrets)
            message = f"{self.name}: {self.executable!r} failed to {action} (exit {completed.returncode})"
            if detail:
                message += f": {detail}"
            raise CredentialStoreError(message)
        return completed

    def _lookup(self, argv: Sequence[str], key: str) -> Optional[str]:
        """Run a read command: absent item -> ``None``, real failure -> raise."""
        completed = self._run_raw(argv)
        if completed.returncode == 0:
            value = completed.stdout or ""
            # Read commands terminate the value with one newline; strip exactly
            # one so a secret that itself ends in a newline survives intact.
            if value.endswith("\n"):
                value = value[:-1]
            return value or None
        detail = _clean_detail(completed)
        if completed.returncode in self.silent_miss_exit_codes and not detail:
            # Real "not found": the tool exited non-zero and said nothing.
            return None
        if _is_not_found(detail):
            return None
        raise CredentialStoreError(
            f"{self.name}: could not look up the credential for {key!r} "
            f"(exit {completed.returncode})" + (f": {detail}" if detail else "")
        )

    def _has_secret_shape(self, key: str) -> bool:
        return isinstance(key, str) and bool(key.strip())


class LinuxSecretServiceStore(_CommandStore):
    """Secret Service (GNOME Keyring, KWallet, ...) through ``secret-tool``."""

    name = "linux-secret-service"
    executable = "secret-tool"

    #: ``secret-tool lookup`` exits 1 and prints nothing for a missing item.
    silent_miss_exit_codes = (1,)

    def availability(self) -> Availability:
        if not sys.platform.startswith("linux"):
            return Availability(False, f"not running on Linux (platform {sys.platform!r})",
                                "use the credential-store backend for this platform")
        if self._command_path() is None:
            return Availability(
                False,
                "the 'secret-tool' command was not found on PATH",
                "install libsecret-tools (Debian/Ubuntu: 'sudo apt install libsecret-tools', "
                "Fedora: 'sudo dnf install libsecret') and make sure a Secret Service such as "
                "gnome-keyring is running in the session; no plaintext fallback is used",
            )
        return Availability(True)

    def _attrs(self, key: str) -> List[str]:
        return ["service", self.service, "account", key]

    def _require_key(self, key: str) -> None:
        if not self._has_secret_shape(key):
            raise CredentialStoreError(f"{self.name}: credential key must be a non-empty string")

    def store(self, key: str, secret: str, *, overwrite: bool = False) -> None:
        self.require_available()
        self._require_key(key)
        self._guard_secret(secret)
        self._guard_overwrite(key, overwrite)
        argv = [self._command_path() or self.executable, "store",
                "--label", f"{self.service} credential for {key}", *self._attrs(key)]
        # secret-tool reads the secret from stdin; it is never placed in argv.
        # It reads one line, so the added newline terminates the value and is
        # not stored; _lookup still strips one trailing newline for tools that
        # do echo it back.
        self._run(argv, stdin_text=secret + "\n", secrets=[secret], action=f"store the credential for {key!r}")

    def retrieve(self, key: str) -> Optional[str]:
        self.require_available()
        self._require_key(key)
        argv = [self._command_path() or self.executable, "lookup", *self._attrs(key)]
        return self._lookup(argv, key)

    def delete(self, key: str) -> bool:
        self.require_available()
        self._require_key(key)
        # ``secret-tool clear`` exits 0 whether or not anything matched, so the
        # exit code alone cannot say whether a credential was removed. Look the
        # item up first and report what actually happened instead of claiming a
        # deletion that did not occur.
        if self.retrieve(key) is None:
            return False
        argv = [self._command_path() or self.executable, "clear", *self._attrs(key)]
        completed = self._run_raw(argv)
        if completed.returncode == 0:
            return True
        detail = _clean_detail(completed)
        if _is_not_found(detail):
            return False
        raise CredentialStoreError(
            f"{self.name}: could not delete the credential for {key!r} "
            f"(exit {completed.returncode})" + (f": {detail}" if detail else "")
        )

    def exists(self, key: str) -> bool:
        self.require_available()
        self._require_key(key)
        return self.retrieve(key) is not None


class MacKeychainStore(_CommandStore):
    """macOS login keychain through the ``security`` CLI."""

    name = "macos-keychain"
    executable = "security"

    def availability(self) -> Availability:
        if sys.platform != "darwin":
            return Availability(False, f"not running on macOS (platform {sys.platform!r})",
                                "use the credential-store backend for this platform")
        if self._command_path() is None:
            return Availability(False, "the 'security' command was not found on PATH",
                                "the 'security' command ships with macOS; check PATH")
        return Availability(True)

    @staticmethod
    def _quote(value: str) -> str:
        """Quote one argument for the ``security -i`` command parser."""
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def store(self, key: str, secret: str, *, overwrite: bool = False) -> None:
        self.require_available()
        self._guard_secret(secret)
        self._guard_overwrite(key, overwrite)
        # ``security add-generic-password ... -w`` with no value prompts on
        # /dev/tty (twice) and ignores piped stdin, so the command is fed to
        # ``security -i`` on stdin instead. The secret therefore never reaches
        # this process's argv, which other users can read from the process list.
        command = "add-generic-password -U -s {service} -a {key} -w {secret}".format(
            service=self._quote(self.service),
            key=self._quote(key),
            secret=self._quote(secret),
        )
        argv = [self._command_path() or self.executable, "-i"]
        self._run(argv, stdin_text=command + "\n", secrets=[secret],
                  action=f"store the credential for {key!r}")

    def retrieve(self, key: str) -> Optional[str]:
        self.require_available()
        argv = [self._command_path() or self.executable, "find-generic-password",
                "-s", self.service, "-a", key, "-w"]
        return self._lookup(argv, key)

    def delete(self, key: str) -> bool:
        self.require_available()
        argv = [self._command_path() or self.executable, "delete-generic-password",
                "-s", self.service, "-a", key]
        completed = self._run_raw(argv)
        if completed.returncode == 0:
            return True
        detail = _clean_detail(completed)
        if _is_not_found(detail):
            return False
        raise CredentialStoreError(
            f"{self.name}: could not delete the credential for {key!r} "
            f"(exit {completed.returncode})" + (f": {detail}" if detail else "")
        )

    def exists(self, key: str) -> bool:
        self.require_available()
        return self.retrieve(key) is not None


class WindowsCredentialStore(CredentialStore):
    """Windows Credential Manager through pywin32 ``win32cred``.

    ``module`` lets a caller (or a test) inject a compatible module; otherwise
    pywin32 is imported lazily so that importing this file works everywhere.
    """

    name = "windows-credential-manager"

    def __init__(self, service: str = DEFAULT_SERVICE, *, module: object = None) -> None:
        self.service = service
        self._module = module

    def target_name(self, key: str) -> str:
        return f"{self.service}:{key}"

    def availability(self) -> Availability:
        if self._module is not None:
            return Availability(True)
        if not sys.platform.startswith("win"):
            return Availability(False, f"not running on Windows (platform {sys.platform!r})",
                                "use the credential-store backend for this platform")
        try:
            import win32cred  # noqa: F401  (existence check only)
        except ImportError as exc:
            return Availability(
                False,
                f"pywin32 ('win32cred') is not importable in this interpreter ({exc})",
                "install it with 'pip install pywin32', or point the setup assistant at another "
                "credential-store backend; no plaintext fallback is used",
            )
        return Availability(True)

    def _win32cred(self):
        if self._module is not None:
            return self._module
        try:
            import win32cred
        except ImportError as exc:  # pragma: no cover - availability() reports this first
            raise CredentialStoreUnavailable(
                f"{self.name}: pywin32 ('win32cred') is not importable in this interpreter ({exc}); "
                "install it with 'pip install pywin32'"
            ) from exc
        self._module = win32cred
        return win32cred

    @staticmethod
    def _decode_blob(blob: object) -> Optional[str]:
        """Decode a CredentialBlob.

        pywin32 returns the blob as raw bytes holding UTF-16-LE text without a
        terminator (verified on Windows: a 55-character secret came back as
        exactly 110 bytes), but some versions return ``str``; handle both, and
        tolerate a stray trailing NUL byte.
        """
        if blob is None:
            return None
        if isinstance(blob, bytes):
            data = blob[:-1] if len(blob) % 2 else blob
            text = data.decode("utf-16-le", "replace")
        else:
            text = str(blob)
        text = text.rstrip("\x00")
        return text or None

    def _read(self, key: str) -> Optional[dict]:
        module = self._win32cred()
        try:
            return module.CredRead(self.target_name(key), module.CRED_TYPE_GENERIC)
        except Exception as exc:  # pywintypes.error carries winerror
            if getattr(exc, "winerror", None) == _WINDOWS_NOT_FOUND:
                return None
            raise CredentialStoreError(
                f"{self.name}: could not read the credential for {key!r}: {exc}"
            ) from exc

    def store(self, key: str, secret: str, *, overwrite: bool = False) -> None:
        self.require_available()
        module = self._win32cred()
        self._guard_secret(secret)
        self._guard_overwrite(key, overwrite)
        try:
            module.CredWrite({
                "Type": module.CRED_TYPE_GENERIC,
                "TargetName": self.target_name(key),
                "UserName": self.service,
                "CredentialBlob": secret,
                "Persist": module.CRED_PERSIST_LOCAL_MACHINE,
                "Comment": f"{self.service} credential for {key}",
            }, 0)
        except Exception as exc:
            raise CredentialStoreError(
                f"{self.name}: could not store the credential for {key!r} in Credential Manager: {exc}"
            ) from exc

    def retrieve(self, key: str) -> Optional[str]:
        self.require_available()
        stored = self._read(key)
        if stored is None:
            return None
        return self._decode_blob(stored.get("CredentialBlob"))

    def delete(self, key: str) -> bool:
        self.require_available()
        module = self._win32cred()
        try:
            module.CredDelete(self.target_name(key), module.CRED_TYPE_GENERIC, 0)
        except Exception as exc:
            if getattr(exc, "winerror", None) == _WINDOWS_NOT_FOUND:
                return False
            raise CredentialStoreError(
                f"{self.name}: could not delete the credential for {key!r}: {exc}"
            ) from exc
        return True

    def exists(self, key: str) -> bool:
        self.require_available()
        return self._read(key) is not None


def default_store(platform_name: Optional[str] = None, service: str = DEFAULT_SERVICE) -> CredentialStore:
    """Return the credential-store backend for ``platform_name`` (default: this host).

    Raises ``CredentialStoreUnavailable`` for a platform with no backend. An
    unsupported platform is an error, never a silent plaintext fallback.
    """
    platform = platform_name or sys.platform
    if platform.startswith("win"):
        return WindowsCredentialStore(service=service)
    if platform == "darwin":
        return MacKeychainStore(service=service)
    if platform.startswith("linux"):
        return LinuxSecretServiceStore(service=service)
    raise CredentialStoreUnavailable(
        f"no credential-store backend for platform {platform!r}; supported platforms are "
        "Windows (Credential Manager), macOS (keychain) and Linux (Secret Service)"
    )
