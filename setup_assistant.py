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
* Failures never make the store worse. If a replaced credential was overwritten
  and the config write then fails, the previous value is restored; a brand-new
  credential is deleted; a failed rollback is reported, never swallowed.
* Optional project services are generic data (``--service NAME=ADDRESS``), not
  hardcoded project or firm defaults.

What the stored secret is for
-----------------------------
The only secret this tool stores is an HTTP **bearer worker credential** for the
office/API endpoint. No module in this kit reads that store automatically yet:
retrieve it with the credential-store backend
(``default_store(service=...).retrieve(key)``) and pass it to ``http_client
--credential``. The ``ssh`` and ``local`` config transports need no secret, so
the setup CLI stores none by default; storing one is an explicit opt-in
(``--store-credential``, or ``--secret-stdin``/the hidden prompt when that flag
is set). The config's ``credential`` block records the store, service, key and
purpose; it never contains the value. Credential keys default to ``<project>``
when no discriminator is known, otherwise ``<project>:<12-hex digest of
actor|checkout>``, so two actors or two checkouts of one project do not share a
key; pass ``--credential-key`` to override.

Config compatibility: the writer produces the same keys ``client.py`` reads for
its ``ssh``/``local`` transports (``transport``, ``host``, ``endpoint``,
``root``, ``python``) plus non-secret bookkeeping keys the client ignores
(``project``, ``checkout``, ``services``, ``credential``).
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('setup_assistant.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)

import argparse
import getpass
import hashlib
import json
import logging
import os
import subprocess
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
    actor: Optional[str] = None
    #: Explicit secret input. ``repr=False`` keeps it out of dataclass reprs/logs.
    secret: Optional[str] = field(default=None, repr=False)
    #: Off by default: the ssh/local transports need no secret, so storing one is
    #: an explicit opt-in (``--store-credential`` on the CLI).
    store_secret: bool = False
    store_service: str = DEFAULT_SERVICE

    def resolved_credential_key(self) -> str:
        """Credential key: an explicit override, else project plus a discriminator.

        ``<project>`` alone is reused by every actor and checkout of a project,
        so when an actor and/or checkout is known the key becomes
        ``<project>:<12-hex sha256 of actor|checkout>``. The digest keeps the key
        short and stable while separating actors and checkouts.
        """
        explicit = (self.credential_key or "").strip()
        if explicit:
            return explicit
        project = (self.project or "").strip()
        discriminators = [value.strip() for value in (self.actor, self.checkout) if (value or "").strip()]
        if not discriminators:
            return project
        digest = hashlib.sha256("|".join(discriminators).encode("utf-8")).hexdigest()[:12]
        return f"{project}:{digest}" if project else digest


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
    config_backup: Optional[str] = None
    messages: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

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
            "config_backup": self.config_backup,
            "dry_run": self.dry_run,
            "messages": list(self.messages),
            "warnings": list(self.warnings),
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
        # The interpreter that runs the endpoint on the SERVER. Only written when asked
        # for: a config without it keeps the client's python3 default, and on RHEL 8 a
        # bare python3 is platform-python 3.6, which cannot run the endpoint
        # (kittrial-5bb.182).
        if request.python:
            config["python"] = request.python
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
            "purpose": (
                "HTTP bearer worker credential for the office/API endpoint. No client reads "
                "this store automatically yet: retrieve it with the credential-store backend "
                "(default_store(service=...).retrieve(key)) and pass it to http_client "
                "--credential. The ssh and local transports do not consume it, and the value "
                "is never stored in this config."
            ),
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


def backup_previous_config(path: os.PathLike) -> Path:
    """Copy an existing config to an owner-only ``.bak`` sibling before replacing it.

    ``--force-config`` is a full replacement, so anything the assistant does not
    manage would otherwise be lost. The previous bytes are preserved verbatim
    (even if they are not valid JSON) with the same owner-only mode as the
    config itself. A second replacement does not overwrite the only backup: the
    new copy gets ``.bak.1``, ``.bak.2``, ... instead.
    """
    target = Path(path).expanduser()
    data = target.read_bytes()
    backup = _available_backup_path(target)
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".bak.tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        try:
            os.chmod(temporary, 0o600)
        except OSError:  # pragma: no cover - Windows ignores most mode bits
            pass
        os.replace(temporary, backup)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return backup


def _available_backup_path(target: Path) -> Path:
    """``<config>.bak`` when free, otherwise ``<config>.bak.N`` (never overwrite)."""
    base = target.with_name(target.name + ".bak")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.name}.bak.{index}")
        if not candidate.exists():
            return candidate
    raise SetupError(
        f"too many existing backups for {target}; move or remove some <name>.bak.N files",
        exit_code=EXIT_ERROR,
    )


def _repository_root(target: Path) -> Optional[Path]:
    """The nearest ancestor holding a ``.git`` entry, or ``None``."""
    for candidate in (target.parent, *target.parent.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_ignores(root: Path, target: Path) -> bool:
    """Whether ``target`` is ignored by the git repository at ``root``.

    A missing or failing git is reported as "not ignored" so the caller still
    warns; this is a warning path and must never break setup.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", str(target)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _repository_path_warning(target: Path, description: str) -> Optional[str]:
    root = _repository_root(target)
    if root is None or _git_ignores(root, target):
        return None
    return (
        f"warning: the {description} {target} is inside the git repository {root} "
        "and is not git-ignored; add it to .gitignore or keep it outside the checkout"
    )


def repository_config_warning(config_path: os.PathLike) -> Optional[str]:
    """A non-fatal warning when the private config sits in a git checkout.

    A private config inside the repository is one ``git add`` away from being
    published. Warn (to stderr, from ``main``) when it is inside a checkout and
    not already ignored; setup still proceeds.
    """
    try:
        target = Path(config_path).expanduser()
    except (TypeError, ValueError):
        return None
    return _repository_path_warning(target, "private config")


def repository_backup_warning(backup_path: os.PathLike) -> Optional[str]:
    """The same warning for the ``.bak`` sibling, which is not covered by an
    ignore rule for the config itself (for example ``client.local.json`` does
    not match ``client.local.json.bak``)."""
    try:
        target = Path(backup_path).expanduser()
    except (TypeError, ValueError):
        return None
    return _repository_path_warning(target, "config backup")


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
    warnings: List[str] = []
    repo_warning = repository_config_warning(config_path)
    if repo_warning:
        warnings.append(repo_warning)

    # A dry run must not require a reachable store, so the default backend is
    # only probed when a credential is actually going to be stored.
    if store is not None:
        resolved_store = store
    else:
        try:
            resolved_store = credential_store.default_store(service=request.store_service)
        except CredentialStoreUnavailable:
            if not dry_run:
                raise
            resolved_store = None
    store = resolved_store
    if store is None:
        store_state: Dict[str, object] = {
            "backend": "none",
            "available": False,
            "reason": "dry run: the credential store was not probed",
        }
    else:
        store_state = store.describe()

    secret: Optional[str] = None
    if request.store_secret:
        if store is not None and not dry_run:
            store.require_available()  # CredentialStoreUnavailable -> handled by main()
        secret = request.secret
        if not secret and interactive:
            secret = getpass_fn(f"Secret for credential {credential_key!r} (input hidden): ")
        if not secret and secret_reader is not None:
            secret = secret_reader()
        if not secret and not dry_run:
            raise SetupError(
                "no secret supplied: pipe the value into --secret-stdin, run "
                "interactively, or omit --store-credential when the setup needs none",
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
    previous_secret: Optional[str] = None
    if request.store_secret and credential_key and store is not None and not dry_run and store.exists(credential_key):
        if confirm_credential or (
            interactive and _confirm(f"A credential for {credential_key!r} already exists. Replace it?", input_fn)
        ):
            replaced_credential = True
            # Read the value being replaced *before* overwriting it, so a failed
            # config write can restore it instead of destroying a working secret.
            previous_secret = store.retrieve(credential_key)
        else:
            raise SetupError(
                f"a credential for {credential_key!r} already exists in {store.name}; "
                "existing credentials are not replaced silently (pass --force-credential)",
                exit_code=EXIT_CREDENTIAL_EXISTS,
            )

    config = build_config(request, store_name=store.name if store is not None else None,
                          credential_key=credential_key)

    if dry_run:
        log_line("dry run: no config written and no credential stored")
        return SetupResult(
            config_path=str(config_path), config=config, store=store_state,
            credential_key=credential_key, secret_stored=False, config_written=False,
            replaced_config=replaced_config, replaced_credential=replaced_credential,
            dry_run=True, messages=tuple(messages), warnings=tuple(warnings),
        )

    config_backup: Optional[str] = None
    if replaced_config:
        try:
            config_backup = str(backup_previous_config(config_path))
        except OSError as exc:
            raise SetupError(
                f"could not back up the existing config {config_path}: {exc}",
                exit_code=EXIT_ERROR,
            ) from exc
        log_line(f"backed up the previous config to {config_backup}")
        # The config's ignore rule usually does not cover the .bak sibling, so
        # check it separately; otherwise it is one `git add .` from publishing.
        backup_warning = repository_backup_warning(config_backup)
        if backup_warning:
            warnings.append(backup_warning)

    stored_now = False
    if request.store_secret and credential_key and secret and store is not None:
        store.store(credential_key, secret, overwrite=replaced_credential)
        stored_now = True
        log_line(
            f"stored credential for {credential_key!r} in {store.name} "
            f"({'replaced' if replaced_credential else 'new'})"
        )

    try:
        write_private_config(config_path, config)
    except BaseException as exc:
        rollback_problem: Optional[BaseException] = None
        if stored_now and credential_key is not None and store is not None:
            try:
                if previous_secret is not None:
                    # A replacement is restored, never deleted.
                    store.store(credential_key, previous_secret, overwrite=True)
                else:
                    # Only a credential this run created is removed.
                    store.delete(credential_key)
            except BaseException as rollback_exc:  # noqa: BLE001 - reported, never swallowed
                rollback_problem = rollback_exc
        if not isinstance(exc, Exception):
            raise
        message = f"could not write the private config {config_path}: {exc}"
        if rollback_problem is not None:
            message += (
                f"; the credential for {credential_key!r} could not be rolled back "
                f"({rollback_problem}), so the store may now hold the new value or none"
            )
        raise SetupError(message, exit_code=EXIT_ERROR) from exc
    log_line(f"wrote private config {config_path} ({'replaced' if replaced_config else 'new'})")
    if request.services:
        log_line(f"recorded {len(request.services)} optional service address(es) as config data")

    return SetupResult(
        config_path=str(config_path), config=config, store=store_state,
        credential_key=credential_key, secret_stored=stored_now, config_written=True,
        replaced_config=replaced_config, replaced_credential=replaced_credential,
        config_backup=config_backup, dry_run=False, messages=tuple(messages),
        warnings=tuple(warnings),
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
            "Write a private client config and, when explicitly requested, store an "
            "HTTP bearer worker credential in the platform credential store. Secrets "
            "are read from a hidden prompt or stdin, never from the command line. The "
            "ssh and local transports need no secret, so none is stored by default."
        ),
    )
    parser.add_argument("--project", help="project name for the private config")
    parser.add_argument("--config", dest="config_path", help="path of the private config to write")
    parser.add_argument("--transport", default="ssh", choices=list(TRANSPORTS))
    parser.add_argument("--host", help="SSH host (ssh transport)")
    parser.add_argument("--endpoint", help="endpoint.py path on the server")
    parser.add_argument("--root", help="Beads runtime root on the server")
    parser.add_argument("--checkout", help="local checkout path recorded in the config")
    parser.add_argument("--actor", help="actor name/id; distinguishes actors in the default credential key")
    parser.add_argument("--python",
                        help="Python interpreter that runs the endpoint: on the server for the ssh "
                             "transport, locally for the local transport (default: python3; an office "
                             "install uses its bundled interpreter under install/current)")
    parser.add_argument("--service", action="append", default=[], metavar="NAME=ADDRESS",
                        help="optional project service address; repeatable")
    parser.add_argument("--credential-key",
                        help="credential-store key (default: <project>, or <project>:<actor/checkout digest>)")
    parser.add_argument("--store-service", default=DEFAULT_SERVICE,
                        help=f"credential-store service namespace (default: {DEFAULT_SERVICE})")
    parser.add_argument("--secret-stdin", action="store_true",
                        help="read the secret from one line on stdin (never from argv); implies --store-credential")
    credential_group = parser.add_mutually_exclusive_group()
    credential_group.add_argument("--store-credential", action="store_true",
                                  help="explicitly opt in to storing a secret in the platform credential store")
    credential_group.add_argument("--no-secret", action="store_true",
                                  help="store no credential at all for this setup (the default)")
    parser.add_argument("--force-config", action="store_true",
                        help="explicitly allow replacing an existing config file (a .bak sibling is kept)")
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
        if args.secret_stdin and args.no_secret:
            raise SetupError("--secret-stdin cannot be combined with --no-secret", exit_code=EXIT_USAGE)
        services = tuple(ServiceSpec.parse(text) for text in args.service)
        if args.secret_stdin:
            secret = _read_stdin_secret(stdin)
        # Storing is opt-in: piping a secret or naming --store-credential is the
        # explicit request; the ssh/local transports otherwise need none.
        store_secret = bool(args.store_credential or args.secret_stdin) and not args.no_secret
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
            actor=args.actor,
            secret=secret,
            store_secret=store_secret,
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
    except OSError as exc:
        # A filesystem failure must be a message and a non-zero exit, not a traceback.
        print(f"setup: {redact_secrets(str(exc), [secret])}", file=stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - last-resort reporting for the CLI
        print(f"setup: {redact_secrets(str(exc), [secret])}", file=stderr)
        return EXIT_ERROR

    for warning in result.warnings:
        print(warning, file=stderr)
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
