"""Project setup assistant: private config plus platform credential storage.

This module turns explicit answers into a reviewable, private client config and
puts any secret in the operating system's credential store instead of in a
file, a log or a command line. It is deliberately additive: nothing in the
existing client path changes, and the CLI below can be wired into the client
later without touching other modules.

Two entry points
----------------
* ``run_setup(request, ...) -> SetupResult`` for non-interactive callers and
  tests; every value is passed in explicitly, and nothing is prompted unless
  ``interactive=True`` is requested.
* ``main(argv) -> int`` for the command line
  (``python setup_assistant.py --help``).

Rules that the code enforces
----------------------------
* Explicit input only. A required value that is missing fails cleanly with exit
  code 2 (never a traceback), and secrets are only ever read from a hidden
  prompt or stdin, never from ``argv`` (argv is visible in the process list).
* No credential echo or logging. The secret is never printed, logged, put in
  the config file or included in an error message; all output passes through
  ``credential_store.redact_secrets`` as a second line of defence.
* No silent overwrite. An existing config file or an existing stored credential
  is only replaced after explicit confirmation (``--force-config`` /
  ``--force-credential``, or an interactive ``y`` answer).
* No plaintext fallback. An unavailable platform store is reported with an
  actionable message (exit code 5) before anything is written.
* Optional project services are generic data (``--service NAME=ADDRESS``), not
  hardcoded project or firm defaults.

Config compatibility: the writer produces the same keys ``client.py`` reads for
its ``ssh``/``local`` transports (``transport``, ``host``, ``endpoint``,
``root``, ``python``) plus non-secret bookkeeping keys the client ignores
(``project``, ``checkout``, ``services``, ``credential``).
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import credential_store
from credential_store import (
    DEFAULT_SERVICE,
    CredentialExistsError,
    CredentialStore,
    CredentialStoreError,
    CredentialStoreUnavailable,
    redact_secrets,
)

LOGGER = logging.getLogger("orchestra.setup")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG_EXISTS = 3
EXIT_CREDENTIAL_EXISTS = 4
EXIT_STORE_UNAVAILABLE = 5

#: Transports accepted by the client config (must match ``client.TRANSPORTS``).
TRANSPORTS = ("ssh", "local")

CONFIG_SCHEMA_VERSION = 1


class SetupError(Exception):
    """A setup step failed; ``exit_code`` is the intended process status."""

    def __init__(self, message: str, *, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ServiceSpec:
    """An optional project service address supplied as plain data."""

    name: str
    address: str
    notes: str = ""

    @classmethod
    def parse(cls, text: str) -> "ServiceSpec":
        """Parse ``NAME=ADDRESS`` or ``NAME=ADDRESS|NOTES``."""
        name, separator, rest = str(text).partition("=")
        name = name.strip()
        address, _, notes = rest.partition("|")
        address = address.strip()
        if not separator or not name or not address:
            raise SetupError(
                f"service {text!r} is not NAME=ADDRESS (optionally NAME=ADDRESS|NOTES)",
                exit_code=EXIT_USAGE,
            )
        return cls(name=name, address=address, notes=notes.strip())

    def as_dict(self) -> Dict[str, str]:
        described = {"name": self.name, "address": self.address}
        if self.notes:
            described["notes"] = self.notes
        return described


@dataclass
class SetupRequest:
    """Explicit setup input. No value is guessed and no secret is stored here."""

    project: str = ""
    config_path: str = ""
    transport: str = "ssh"
    host: Optional[str] = None
    endpoint: Optional[str] = None
    root: Optional[str] = None
    checkout: Optional[str] = None
    python: Optional[str] = None
    services: Tuple[ServiceSpec, ...] = ()
    credential_key: Optional[str] = None
    #: Explicit secret input. ``repr=False`` keeps it out of dataclass reprs/logs.
    secret: Optional[str] = field(default=None, repr=False)
    store_secret: bool = True
    store_service: str = DEFAULT_SERVICE

    def resolved_credential_key(self) -> str:
        return (self.credential_key or self.project or "").strip()


@dataclass
class SetupResult:
    """Secret-free outcome of a setup run."""

    config_path: str
    config: Dict[str, object]
    store: Dict[str, object]
    credential_key: Optional[str] = None
    secret_stored: bool = False
    config_written: bool = False
    replaced_config: bool = False
    replaced_credential: bool = False
    dry_run: bool = False
    messages: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {
            "config_path": self.config_path,
            "config": self.config,
            "credential": {
                "key": self.credential_key,
                "stored": self.secret_stored,
                "replaced": self.replaced_credential,
                "store": self.store,
            },
            "config_written": self.config_written,
            "replaced_config": self.replaced_config,
            "dry_run": self.dry_run,
            "messages": list(self.messages),
        }


def missing_required(request: SetupRequest) -> List[str]:
    """Names of required options that the request does not supply, in CLI form."""
    missing: List[str] = []
    if not (request.project or "").strip():
        missing.append("--project")
    if not (request.config_path or "").strip():
        missing.append("--config")
    transport = (request.transport or "").strip()
    if transport not in TRANSPORTS:
        missing.append("--transport")
        return missing
    if transport == "ssh":
        if not (request.host or "").strip():
            missing.append("--host")
        if not (request.endpoint or "").strip():
            missing.append("--endpoint")
        if not (request.root or "").strip():
            missing.append("--root")
    else:
        if not (request.endpoint or "").strip():
            missing.append("--endpoint")
        if not (request.root or "").strip():
            missing.append("--root")
    if request.store_secret and not (request.credential_key or request.project or "").strip():
        missing.append("--credential-key")
    return missing


def build_config(
    request: SetupRequest,
    *,
    store_name: Optional[str] = None,
    credential_key: Optional[str] = None,
) -> Dict[str, object]:
    """The private, non-secret config for ``request`` (never contains a secret)."""
    transport = (request.transport or "ssh").strip()
    config: Dict[str, object] = {"schema_version": CONFIG_SCHEMA_VERSION, "transport": transport}
    if transport == "ssh":
        config["host"] = request.host
        config["endpoint"] = request.endpoint
        config["root"] = request.root
    else:
        config["endpoint"] = request.endpoint
        config["root"] = request.root
        config["python"] = request.python or "python3"
    if request.project:
        config["project"] = request.project
    if request.checkout:
        config["checkout"] = request.checkout
    if request.services:
        config["services"] = [service.as_dict() for service in request.services]
    if request.store_secret and credential_key:
        config["credential"] = {
            "store": store_name or "platform",
            "service": request.store_service,
            "key": credential_key,
        }
    return config


def write_private_config(path: os.PathLike, config: Dict[str, object]) -> Path:
    """Write ``config`` as JSON to ``path`` atomically and owner-only.

    The file is created with mode 0600 (best effort on Windows, where access is
    governed by the directory's ACLs) through a temporary file in the same
    directory, so a reader never sees a half-written config.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config, indent=2, sort_keys=False) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        try:
            os.chmod(temporary, 0o600)
        except OSError:  # pragma: no cover - Windows ignores most mode bits
            pass
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def _prompt_for_missing(
    request: SetupRequest, input_fn: Callable[[str], str]
) -> None:
    """Fill missing required values from interactive prompts (labels mirror the CLI)."""
    labels = {
        "--project": ("project", "Project name"),
        "--config": ("config_path", "Private config path"),
        "--transport": ("transport", "Transport (ssh or local)"),
        "--host": ("host", "SSH host"),
        "--endpoint": ("endpoint", "Endpoint path on the server"),
        "--root": ("root", "Runtime root on the server"),
    }
    for option in missing_required(request):
        attribute, label = labels.get(option, (None, None))
        if attribute is None:
            continue
        answer = input_fn(f"{label}: ").strip()
        if answer:
            setattr(request, attribute, answer)


def _confirm(question: str, input_fn: Callable[[str], str]) -> bool:
    return input_fn(f"{question} [y/N]: ").strip().lower() in ("y", "yes")


def _redacting_log(messages: List[str], secret: Optional[str], log: Callable[[str], None]) -> Callable[[str], None]:
    def emit(message: str) -> None:
        safe = redact_secrets(message, [secret])
        messages.append(safe)
        log(safe)

    return emit


def run_setup(
    request: SetupRequest,
    *,
    store: Optional[CredentialStore] = None,
    interactive: Optional[bool] = None,
    confirm_config: bool = False,
    confirm_credential: bool = False,
    dry_run: bool = False,
    secret_reader: Optional[Callable[[], str]] = None,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    log: Optional[Callable[[str], None]] = None,
) -> SetupResult:
    """Run one setup with explicit input; return a secret-free result.

    ``interactive=None`` means "prompt only when both stdin and stdout are a
    terminal". Passing ``interactive=False`` (the CLI's ``--non-interactive``)
    guarantees that a missing value or missing secret fails instead of hanging.
    """
    if interactive is None:
        interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    if interactive:
        _prompt_for_missing(request, input_fn)

    missing = missing_required(request)
    if missing:
        raise SetupError(
            "missing required input: " + ", ".join(missing)
            + " (supply it as an option, or run interactively to be prompted)",
            exit_code=EXIT_USAGE,
        )

    config_path = Path(request.config_path).expanduser()
    credential_key = request.resolved_credential_key() if request.store_secret else None
    messages: List[str] = []

    store = store if store is not None else credential_store.default_store(service=request.store_service)
    store_state = store.describe()

    secret: Optional[str] = None
    if request.store_secret:
        store.require_available()  # CredentialStoreUnavailable -> handled by main()
        secret = request.secret
        if not secret and interactive:
            secret = getpass_fn(f"Secret for credential {credential_key!r} (input hidden): ")
        if not secret and secret_reader is not None:
            secret = secret_reader()
        if not secret and not dry_run:
            raise SetupError(
                "no secret supplied: pipe the value into --secret-stdin, run "
                "interactively, or pass --no-secret when the setup needs none",
                exit_code=EXIT_USAGE,
            )

    # Redaction is applied to every message once the secret is known.
    log_line = _redacting_log(messages, secret, log or LOGGER.info)

    replaced_config = False
    if config_path.exists():
        if confirm_config or (interactive and _confirm(f"Config {config_path} already exists. Overwrite it?", input_fn)):
            replaced_config = True
        else:
            raise SetupError(
                f"config {config_path} already exists; existing installations are not "
                "overwritten silently (pass --force-config to replace it)",
                exit_code=EXIT_CONFIG_EXISTS,
            )

    replaced_credential = False
    if request.store_secret and credential_key and store.exists(credential_key):
        if confirm_credential or (
            interactive and _confirm(f"A credential for {credential_key!r} already exists. Replace it?", input_fn)
        ):
            replaced_credential = True
        else:
            raise SetupError(
                f"a credential for {credential_key!r} already exists in {store.name}; "
                "existing credentials are not replaced silently (pass --force-credential)",
                exit_code=EXIT_CREDENTIAL_EXISTS,
            )

    config = build_config(request, store_name=store.name, credential_key=credential_key)

    if dry_run:
        log_line(f"dry run: no config written and no credential stored ({store.name})")
        return SetupResult(
            config_path=str(config_path), config=config, store=store_state,
            credential_key=credential_key, secret_stored=False, config_written=False,
            replaced_config=replaced_config, replaced_credential=replaced_credential,
            dry_run=True, messages=tuple(messages),
        )

    stored_now = False
    if request.store_secret and credential_key and secret:
        store.store(credential_key, secret, overwrite=replaced_credential)
        stored_now = True
        log_line(
            f"stored credential for {credential_key!r} in {store.name} "
            f"({'replaced' if replaced_credential else 'new'})"
        )

    try:
        write_private_config(config_path, config)
    except BaseException:
        if stored_now:
            # Do not leave a credential behind for a config that was not written.
            try:
                store.delete(credential_key)
            except CredentialStoreError:
                pass
        raise
    log_line(f"wrote private config {config_path} ({'replaced' if replaced_config else 'new'})")
    if request.services:
        log_line(f"recorded {len(request.services)} optional service address(es) as config data")

    return SetupResult(
        config_path=str(config_path), config=config, store=store_state,
        credential_key=credential_key, secret_stored=stored_now, config_written=True,
        replaced_config=replaced_config, replaced_credential=replaced_credential,
        dry_run=False, messages=tuple(messages),
    )


def _read_stdin_secret(stream) -> str:
    line = stream.readline()
    if line == "":
        raise SetupError("no secret on stdin; pipe the value into --secret-stdin", exit_code=EXIT_USAGE)
    return line.rstrip("\r\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_assistant.py",
        description=(
            "Write a private client config and store its secret in the platform "
            "credential store. Secrets are read from a hidden prompt or stdin, "
            "never from the command line."
        ),
    )
    parser.add_argument("--project", help="project name for the private config")
    parser.add_argument("--config", dest="config_path", help="path of the private config to write")
    parser.add_argument("--transport", default="ssh", choices=list(TRANSPORTS))
    parser.add_argument("--host", help="SSH host (ssh transport)")
    parser.add_argument("--endpoint", help="endpoint.py path on the server")
    parser.add_argument("--root", help="Beads runtime root on the server")
    parser.add_argument("--checkout", help="local checkout path recorded in the config")
    parser.add_argument("--python", help="Python interpreter for the local transport")
    parser.add_argument("--service", action="append", default=[], metavar="NAME=ADDRESS",
                        help="optional project service address; repeatable")
    parser.add_argument("--credential-key", help="credential-store key (default: the project name)")
    parser.add_argument("--store-service", default=DEFAULT_SERVICE,
                        help=f"credential-store service namespace (default: {DEFAULT_SERVICE})")
    parser.add_argument("--secret-stdin", action="store_true",
                        help="read the secret from one line on stdin (never from argv)")
    parser.add_argument("--no-secret", action="store_true",
                        help="store no credential at all for this setup")
    parser.add_argument("--force-config", action="store_true",
                        help="explicitly allow replacing an existing config file")
    parser.add_argument("--force-credential", action="store_true",
                        help="explicitly allow replacing an existing stored credential")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print the config without writing or storing anything")
    parser.add_argument("--non-interactive", action="store_true",
                        help="never prompt; fail with exit code 2 when input is missing")
    parser.add_argument("--json", action="store_true", help="print the secret-free result as JSON")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    store: Optional[CredentialStore] = None,
    log: Optional[Callable[[str], None]] = None,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    stdin=None,
    stdout=None,
    stderr=None,
) -> int:
    """CLI entry point. Returns the process exit code; never echoes a secret."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    secret: Optional[str] = None
    try:
        services = tuple(ServiceSpec.parse(text) for text in args.service)
        if args.secret_stdin:
            secret = _read_stdin_secret(stdin)
        interactive = False if args.non_interactive else bool(stdin.isatty() and stdout.isatty())
        request = SetupRequest(
            project=args.project or "",
            config_path=args.config_path or "",
            transport=args.transport,
            host=args.host,
            endpoint=args.endpoint,
            root=args.root,
            checkout=args.checkout,
            python=args.python,
            services=services,
            credential_key=args.credential_key,
            secret=secret,
            store_secret=not args.no_secret,
            store_service=args.store_service,
        )
        result = run_setup(
            request,
            store=store,
            interactive=interactive,
            confirm_config=args.force_config,
            confirm_credential=args.force_credential,
            dry_run=args.dry_run,
            secret_reader=None,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            log=log,
        )
    except SetupError as exc:
        message = redact_secrets(str(exc), [secret])
        print(f"setup: {message}", file=stderr)
        return exc.exit_code
    except CredentialStoreUnavailable as exc:
        print(f"setup: {redact_secrets(str(exc), [secret])}", file=stderr)
        return EXIT_STORE_UNAVAILABLE
    except CredentialExistsError as exc:
        print(f"setup: {redact_secrets(str(exc), [secret])}", file=stderr)
        return EXIT_CREDENTIAL_EXISTS
    except CredentialStoreError as exc:
        print(f"setup: {redact_secrets(str(exc), [secret])}", file=stderr)
        return EXIT_ERROR

    if log is None:
        for message in result.messages:
            print(message, file=stdout)
    if args.dry_run:
        print(json.dumps(result.config, indent=2, sort_keys=True), file=stdout)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True), file=stdout)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
