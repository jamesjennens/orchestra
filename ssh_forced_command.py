#!/usr/bin/env python3
"""Confine one SSH key to the coordination endpoint: an authorized_keys wrapper.

Install this as the ``command=`` value of an ``authorized_keys`` entry, for example
(one line; ``admin.py authorized-keys`` prints the exact text for a key):

    command="/usr/bin/python3 -E -s /home/beads/beads-team-kit/ssh_forced_command.py \
--root /home/beads/beads-runtime \
--endpoint /home/beads/beads-team-kit/endpoint.py",restrict,no-pty,no-port-forwarding,\
no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... contributor

The interpreter is absolute and runs ``-E -s`` (ignore ``PYTHON*`` variables, ignore the
user site directory; not ``-I``, which would also drop the script's directory from
``sys.path``). ``restrict`` leads the options so the user rc file and any capability a
later OpenSSH adds are off by default.

Why: ``client.py`` runs the endpoint as ``ssh HOST "PYTHON endpoint.py --root ROOT"``,
where ``PYTHON`` is the interpreter the client config names (``python3`` when it names
none), and the endpoint takes its HTTP authority from *its own* launch flags, so every
authority rule in the kit (the operator allowlist on host commands, writes that stay
``unverified``, the reserved comment prefixes, the HTTP actor-shape reservation) binds
only a caller who cannot choose that command line. A contributor key with an ordinary
service-account shell can run ``admin.py`` or ``bd`` instead. This wrapper is the
boundary that makes those rules real: the key can run the endpoint and nothing else.

The contract, deliberately narrow:

* The wrapper's *own* argv (``--root``, one or more ``--endpoint``, ``--python``, and the
  ``--project`` names a key is bound to) is the only trusted input. It comes from the sshd
  configuration, not from the connection.
* ``SSH_ORIGINAL_COMMAND`` is never executed and never becomes part of a command line.
  It is read only to *select* the endpoint: after ``shlex.split`` it must be exactly one
  token equal to one of the configured ``--endpoint`` paths. Every other value - another
  program (``admin.py``, ``bd``, a shell), an extra token, any flag (``--root``,
  ``--authority-store``, ``--authority-lock``, ``--require-authority``), an empty command
  or an unbalanced quote - is refused on stderr with status 2 and nothing is executed.
* On a match the wrapper replaces itself with exactly
  ``[python, endpoint, '--root', <the fixed root>]``, so ``--root`` is fixed by the
  deployment and no authority flag can be passed. stdin, stdout and stderr (the JSON
  request/response envelope) pass through unchanged.
* A key may be BOUND to projects (kittrial-5bb.193, rule 1 of
  ``docs/COORDINATORS_PER_PROJECT_DESIGN.md``): each ``--project NAME`` of the line is
  handed to the endpoint as ``--key-project NAME`` after the root, and the endpoint then
  refuses every request that names another project. The project stays in the request; the
  caller cannot add a name or drop one, because nothing of the connection reaches this
  command line. A line with no ``--project`` starts the endpoint exactly as before.
* A key may be BOUND to a PRINCIPAL (kittrial-5bb.194, rule 2): ``--principal NAME`` of the
  line (a lane, ``lane:NAME`` or ``person:NAME``; the two prefixes are different principals)
  is handed to the endpoint as ``--key-principal NAME``, and the endpoint then refuses every
  request whose actor that principal does not own in the project - except a session
  registration, which makes the new actor that principal's. A line with no ``--principal``
  starts the endpoint exactly as before, and looks at no owner. A line that names a principal
  twice is refused, like a project named twice.
* An installation may accept BOUND KEYS ONLY (kittrial-5bb.196, slice 4 of
  ``docs/COORDINATORS_PER_PROJECT_DESIGN.md``). The setting is one key,
  ``bound_keys_only``, in the runtime's own ``deployment.private.json`` (the file ``--root``
  names; it cannot come from the connection, and nothing of the line carries it). With it
  true, a line that names no project or no principal is refused here, before the endpoint is
  selected or run: the line cannot say which projects it may use or whose actors it may be,
  and the setting exists so that such a line is not served. A line that names both is served
  exactly as before, and with the setting false or absent nothing changes at all.
* The endpoint is exec'd with a minimal, explicit environment: ``PATH``, ``HOME``, the
  locale variables (``LANG``, ``LC_*``), and the variables this kit sets for the endpoint
  itself (none today). Everything else the session carried - ``PYTHONPATH``, ``BASH_ENV``,
  ``ENV``, ``LD_PRELOAD``, ``SSH_ORIGINAL_COMMAND`` - is dropped. sshd applies
  ``PermitUserEnvironment`` and ``AcceptEnv`` *before* ``command=`` runs, so a forwarded
  variable still reaches the interpreter that starts this wrapper (and ``BASH_ENV`` the
  shell before it); what the wrapper can and does close is the endpoint's own process. See
  ``docs/OPERATIONS.md`` ("sshd settings the boundary needs").

The key this confines must belong to a contributor whose client config sets
``"forced_command": true``: that makes the client send only the endpoint path, because
the ``--root`` the old command line carried is refused here. See
``docs/OPERATIONS.md`` ("Confine contributor keys with a forced command").
"""
import argparse
import json
import os
import posixpath  # the paths here are the server's POSIX paths, whatever platform runs the tests
import re
import shlex
import stat
import sys
from pathlib import Path

REFUSAL = 'ssh_forced_command: '
EXIT_REFUSED = 2
# Bounded, so an unbounded or control-character-laden caller string never floods a log.
ECHO_LIMIT = 120
# One bare interpreter name (`python3`) or an absolute path; the same class admin.py's
# authorized-keys helper prints, so the two never disagree about what a safe argv is.
PYTHON_NAME = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*|/[A-Za-z0-9_./-]+)')
# Variables this kit sets for the endpoint process itself. Empty today: endpoint.py reads
# none. A future kit component that needs one adds it here deliberately, so nothing a
# caller can forward (via AcceptEnv) reaches the endpoint by inheritance.
KIT_ENVIRONMENT = {}
# A project name as the kit makes them (admin.validate_name; not imported here, so that this
# wrapper stays one file that starts on any interpreter the account has).
PROJECT_NAME = re.compile(r'[a-z][a-z0-9]{1,23}')
# A principal name (kittrial-5bb.194): a lane, spelled `lane:NAME` or `person:NAME` (the
# coordinator decision of 2026-10-08; the two prefixes are different principals), one token
# with no space, because it is an argument of the authorized_keys command line and that line
# is split on spaces. Kept here as a regex for the same one-file reason.
PRINCIPAL_NAME = re.compile(r'(?:lane|person):[A-Za-z0-9][A-Za-z0-9_.-]{0,94}')
# Forwarded from the session because the kit needs them: PATH to find the interpreter,
# HOME for the account's own files, and the locale so text handling matches the terminal.
LOCALE_NAMES = ('LANG',)
LOCALE_PREFIX = 'LC_'
# The runtime's own settings file, and the key in it of the installation setting that accepts
# bound keys only (kittrial-5bb.196, slice 4). ``admin.py`` reads the same file and the same
# key for its `bound-keys-only` command and for `setup-status`; this wrapper cannot import it
# (it must start on any interpreter the account has), so it has this small reader of its own,
# pinned against the host reader by tests/test_bound_keys_only.py.
SETTINGS_FILE = 'deployment.private.json'
BOUND_KEYS_ONLY = 'bound_keys_only'


def default_endpoint():
    """The endpoint shipped next to this wrapper, used when --endpoint is omitted."""
    return str(Path(__file__).resolve().parent / 'endpoint.py')


def default_python():
    """The interpreter that is already running this wrapper, or /usr/bin/python3.

    Absolute on purpose: a bare name would be resolved through PATH, which the session's
    forwarded environment can move.
    """
    executable = getattr(sys, 'executable', '') or ''
    if posixpath.isabs(executable) and PYTHON_NAME.fullmatch(executable):
        return executable
    return '/usr/bin/python3'


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='ssh_forced_command.py',
        description='Run one configured endpoint, with a fixed --root and no authority flags.',
        # No prefix matching: `--end` must not silently select `--endpoint`, and an
        # abbreviation introduced by a later option must not change this line's meaning.
        allow_abbrev=False,
    )
    parser.add_argument('--root', required=True,
                        help='the fixed runtime root passed to the endpoint (never the caller\'s)')
    parser.add_argument('--endpoint', action='append', default=[], metavar='PATH',
                        help='an endpoint path a caller may select (repeatable; defaults to '
                             'endpoint.py beside this wrapper)')
    parser.add_argument('--python', default=None,
                        help='interpreter used for the endpoint (default: this wrapper\'s own)')
    parser.add_argument('--project', action='append', default=[], metavar='NAME',
                        help='a project this key is bound to (repeatable); with none the key may '
                             'name any project, as before')
    parser.add_argument('--principal', action='append', default=None, metavar='NAME',
                        help='the principal (a lane, lane:NAME or person:NAME) this key belongs to; '
                             'with none the key acts as any actor, as before (given twice, refused)')
    return parser.parse_args(argv)


def _path(value, label):
    if (not isinstance(value, str) or not value or not posixpath.isabs(value)
            or any(character in value for character in '\0\r\n')):
        raise ValueError('%s must be one absolute path with no control characters' % label)
    return value


def _python(value):
    if not isinstance(value, str) or not PYTHON_NAME.fullmatch(value):
        raise ValueError('--python must be one interpreter name or absolute path using only '
                         'letters, digits, dot, underscore, dash and slash')
    return value


def _projects(values):
    """The projects of a bound line, in the order written; an empty list for an unbound one."""
    projects = []
    for value in values or []:
        if not isinstance(value, str) or not PROJECT_NAME.fullmatch(value):
            raise ValueError('--project must be a project name (2-24 lowercase letters and digits, '
                             'beginning with a letter); refused %s' % _echo(value))
        if value in projects:
            raise ValueError('--project names %s twice' % value)
        projects.append(value)
    return projects


def _principal(value):
    """The principal of a bound line, or None for an unbound one.

    ``--principal`` is an append argument, so a line that names one twice is refused here
    rather than silently taking the last: the listing and the wrapper must agree about which
    principal a line binds (kittrial-5bb.194 review, finding 3; slice 1 refuses a project
    named twice the same way).
    """
    if isinstance(value, list):
        if len(value) > 1:
            raise ValueError('--principal names %s twice; a line may name at most one principal'
                             % _echo(', '.join(str(item) for item in value)))
        value = value[0] if value else None
    if value is None:
        return None
    if not isinstance(value, str) or not PRINCIPAL_NAME.fullmatch(value):
        raise ValueError('--principal must be a principal name of the form lane:NAME or person:NAME (one token: '
                         'letters, digits, dot, underscore, dash; no space); refused %s' % _echo(value))
    return value


def configured(args):
    """Return (root, endpoints, python) as validated strings, or raise ValueError.

    Unknown flags (for example an HTTP service's ``--authority-store`` copied into the
    authorized_keys line by mistake) are refused by argparse before this runs: a
    contribution key must never reach the authority path by accident.
    """
    root = _path(args.root, '--root')
    endpoints = [_path(item, '--endpoint') for item in (args.endpoint or [default_endpoint()])]
    python = _python(default_python() if args.python is None else args.python)
    return root, endpoints, python


def child_environment(environment=None):
    """The exact environment the endpoint is exec'd with: explicit, minimal, no inheritance.

    Everything not named here is dropped, so a caller who can influence the session
    environment (``AcceptEnv``/``PermitUserEnvironment``) cannot plant ``PYTHONPATH``,
    ``BASH_ENV``, ``ENV``, ``LD_PRELOAD`` or any other variable in the endpoint process.
    """
    source = os.environ if environment is None else environment
    forwarded = {name: source[name] for name in sorted(source)
                 if name in LOCALE_NAMES or name.startswith(LOCALE_PREFIX)}
    forwarded['PATH'] = source.get('PATH') or os.defpath
    if source.get('HOME'):
        forwarded['HOME'] = source['HOME']
    forwarded.update(KIT_ENVIRONMENT)
    return forwarded


def bound_keys_only(root):
    """Whether the installation at ``root`` accepts bound keys only (kittrial-5bb.196).

    Read from ``<root>/deployment.private.json``, the file ``admin.py bound-keys-only on``
    writes: the setting is the installation's, ``--root`` is fixed by the authorized_keys line
    and never by the connection, and the wrapper's own argv cannot carry it (the line's
    ``--project``/``--principal`` are exactly what the setting is about).

    OFF is the absent key, so an installation that configures nothing reads as it always did
    and a fresh install and a rolled-back one read identically. A value that is neither true
    nor false reads as OFF with a warning on stderr: the same rule ``admin.py``'s
    ``review_workflow_writes`` follows for its own switch (kittrial-5bb.110 item 2), so one
    mistyped value cannot refuse every key of the installation. A settings file that cannot be
    read as the kit's configuration - not a regular file, unreadable, not text, not JSON, not a
    JSON object - is refused instead: the wrapper cannot tell whether the setting is on, and
    reading that as OFF would silently drop the installation's rule. The operator's own
    unrestricted key does not run this wrapper, so this can never lock an operator out.
    """
    marker = posixpath.join(root, SETTINGS_FILE)
    try:
        mode = os.stat(marker).st_mode
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError('cannot read the installation setting %s: %s' % (marker, error)) from None
    if not stat.S_ISREG(mode):
        raise ValueError('the installation setting %s is not a regular file' % marker)
    try:
        document = json.loads(Path(marker).read_text(encoding='utf-8'))
    except (OSError, UnicodeError) as error:
        raise ValueError('cannot read the installation setting %s: %s' % (marker, error)) from None
    except RecursionError:
        raise ValueError('the installation setting %s nests too deeply to read' % marker) from None
    except ValueError:
        raise ValueError('the installation setting %s is not valid JSON' % marker) from None
    if not isinstance(document, dict):
        raise ValueError('the installation setting %s is not a JSON object' % marker)
    value = document.get(BOUND_KEYS_ONLY)
    if isinstance(value, bool):
        return value
    if value is not None:
        sys.stderr.write('%sWARNING: deployment %s is %s, not true or false, so this installation is '
                         'NOT refusing a line without a project or a principal; correct the value in %s\n'
                         % (REFUSAL, BOUND_KEYS_ONLY, _echo(value), marker))
    return False


def _echo(value):
    text = repr(value)
    return text if len(text) <= ECHO_LIMIT else text[:ECHO_LIMIT] + '...'


def select_endpoint(tokens, endpoints):
    """Pick the endpoint the caller selected. Returns (path, None) or (None, reason)."""
    if not tokens:
        return None, ('no endpoint selected: this key runs only the configured endpoint; '
                      'a client with "forced_command": true sends its path')
    if len(tokens) != 1:
        return None, ('this key runs only the configured endpoint and accepts no arguments; '
                      'refused %s' % _echo(' '.join(tokens)))
    chosen = tokens[0]
    if chosen not in endpoints:
        return None, 'this key may not run %s' % _echo(chosen)
    return chosen, None


def endpoint_argv(python, endpoint, root, projects=(), principal=None):
    """The exact command line the endpoint is launched with: the fixed root, and for a bound
    key the projects of its line and its principal. No other flag, and none of it from the
    connection."""
    command = [python, endpoint, '--root', root]
    for project in projects:
        command.extend(['--key-project', project])
    if principal is not None:
        command.extend(['--key-principal', principal])
    return command


def refuse(reason):
    sys.stderr.write(REFUSAL + reason + '\n')
    return EXIT_REFUSED


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else list(argv))
        root, endpoints, python = configured(args)
        projects = _projects(args.project)
        principal = _principal(args.principal)
        # An installation set to accept bound keys only (kittrial-5bb.196) refuses a line that
        # names no project or no principal, before anything of the connection is looked at (so
        # an `ssh -T HOST` check answers this setting too) and before any program runs. It is
        # read from the runtime the line itself names, so a line printed before the setting was
        # turned on is refused as well.
        if bound_keys_only(root):
            missing = ' and '.join('no %s' % name for name, given in (('--project', bool(projects)),
                                                                      ('--principal', principal is not None))
                                   if not given)
            if missing:
                return refuse('this installation accepts bound keys only, so an authorized_keys line must '
                              'name at least one project and one principal; this line names %s. Print the '
                              'line again with this kit (admin.py authorized-keys --project NAME --principal '
                              'lane:NAME) and replace it. Nothing was run' % missing)
        try:
            tokens = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        except ValueError as error:
            return refuse('the remote command could not be parsed (%s); nothing was run' % error)
        chosen, reason = select_endpoint(tokens, endpoints)
        if chosen is None:
            return refuse(reason)
        command = endpoint_argv(python, chosen, root, projects, principal)
        # Replace this process: the endpoint inherits the caller's stdio byte for byte, but
        # not the caller-influenced environment (see child_environment).
        os.execvpe(command[0], command, child_environment())
    except ValueError as error:
        return refuse(str(error))
    except OSError as error:
        return refuse('could not run the endpoint: %s' % error)
    return refuse('the endpoint did not replace this process; nothing was run')


if __name__ == '__main__':
    sys.exit(main())
