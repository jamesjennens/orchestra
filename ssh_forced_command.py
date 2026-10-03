#!/usr/bin/env python3
"""Confine one SSH key to the coordination endpoint: an authorized_keys wrapper.

Install this as the ``command=`` value of an ``authorized_keys`` entry, for example
(one line):

    command="/usr/bin/python3 /home/beads/beads-team-kit/ssh_forced_command.py \
--root /home/beads/beads-runtime \
--endpoint /home/beads/beads-team-kit/endpoint.py",no-pty,no-port-forwarding,\
no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... contributor

Why: ``client.py`` runs the endpoint as ``ssh HOST "python3 endpoint.py --root ROOT"``
and the endpoint takes its HTTP authority from *its own* launch flags, so every
authority rule in the kit (the operator allowlist on host commands, writes that stay
``unverified``, the reserved comment prefixes, the HTTP actor-shape reservation) binds
only a caller who cannot choose that command line. A contributor key with an ordinary
service-account shell can run ``admin.py`` or ``bd`` instead. This wrapper is the
boundary that makes those rules real: the key can run the endpoint and nothing else.

The contract, deliberately narrow:

* The wrapper's *own* argv (``--root``, one or more ``--endpoint``, ``--python``) is the
  only trusted input. It comes from the sshd configuration, not from the connection.
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

The key this confines must belong to a contributor whose client config sets
``"forced_command": true``: that makes the client send only the endpoint path, because
the ``--root`` the old command line carried is refused here. See
``docs/OPERATIONS.md`` ("Confine contributor keys with a forced command").
"""
import argparse
import os
import shlex
import sys
from pathlib import Path

REFUSAL = 'ssh_forced_command: '
EXIT_REFUSED = 2
# Bounded, so an unbounded or control-character-laden caller string never floods a log.
ECHO_LIMIT = 120


def default_endpoint():
    """The endpoint shipped next to this wrapper, used when --endpoint is omitted."""
    return str(Path(__file__).resolve().parent / 'endpoint.py')


def default_python():
    """The interpreter that is already running this wrapper, or python3 on PATH."""
    executable = getattr(sys, 'executable', '') or ''
    return executable if os.path.isabs(executable) else 'python3'


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='ssh_forced_command.py',
        description='Run one configured endpoint, with a fixed --root and no authority flags.',
    )
    parser.add_argument('--root', required=True,
                        help='the fixed runtime root passed to the endpoint (never the caller\'s)')
    parser.add_argument('--endpoint', action='append', default=[], metavar='PATH',
                        help='an endpoint path a caller may select (repeatable; defaults to '
                             'endpoint.py beside this wrapper)')
    parser.add_argument('--python', default=None,
                        help='interpreter used for the endpoint (default: this wrapper\'s own)')
    return parser.parse_args(argv)


def _path(value, label):
    if (not isinstance(value, str) or not value or not os.path.isabs(value)
            or any(character in value for character in '\0\r\n')):
        raise ValueError('%s must be one absolute path with no control characters' % label)
    return value


def configured(args):
    """Return (root, endpoints, python) as validated strings, or raise ValueError.

    Unknown flags (for example an HTTP service's ``--authority-store`` copied into the
    authorized_keys line by mistake) are refused by argparse before this runs: a
    contribution key must never reach the authority path by accident.
    """
    root = _path(args.root, '--root')
    endpoints = [_path(item, '--endpoint') for item in (args.endpoint or [default_endpoint()])]
    python = args.python or default_python()
    if not isinstance(python, str) or not python or any(c in python for c in '\0\r\n'):
        raise ValueError('--python must be one interpreter path with no control characters')
    return root, endpoints, python


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


def endpoint_argv(python, endpoint, root):
    """The exact command line the endpoint is launched with: fixed root, no other flag."""
    return [python, endpoint, '--root', root]


def refuse(reason):
    sys.stderr.write(REFUSAL + reason + '\n')
    return EXIT_REFUSED


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else list(argv))
        root, endpoints, python = configured(args)
        try:
            tokens = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        except ValueError as error:
            return refuse('the remote command could not be parsed (%s); nothing was run' % error)
        chosen, reason = select_endpoint(tokens, endpoints)
        if chosen is None:
            return refuse(reason)
        command = endpoint_argv(python, chosen, root)
        # Replace this process: the endpoint inherits the caller's stdio byte for byte.
        os.execvp(command[0], command)
    except ValueError as error:
        return refuse(str(error))
    except OSError as error:
        return refuse('could not run the endpoint: %s' % error)
    return refuse('the endpoint did not replace this process; nothing was run')


if __name__ == '__main__':
    sys.exit(main())
