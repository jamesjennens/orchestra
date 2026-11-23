"""Shared Dolt lifecycle and readiness helpers for foreground office runtimes (kittrial-5bb.520).

Used by both ``office_service.py`` (the foreground supervisor) and ``admin.py``
(temporary Dolt during ``restore-new`` on a stopped ``office-foreground`` runtime) so
process spawning, parent-death protection, shutdown, credential stripping, startup wait
configuration, and ``SELECT 1`` readiness probing follow one rule and cannot drift.

Keep this module free of top-level POSIX-only imports (so Windows test runners can import
it) and free of Python 3.7+ syntax or stdlib APIs (the office host runs Python 3.6).
"""
import csv
import ctypes
import errno
import io
import os
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

import record_json

DOLT_STARTUP_SECONDS_KEY = 'dolt_startup_seconds'
DEFAULT_DOLT_STARTUP_SECONDS = 30
MIN_DOLT_STARTUP_SECONDS = 1
MAX_DOLT_STARTUP_SECONDS = 600
DOLT_STOP_SECONDS = 5
SQL_PROBE_TIMEOUT = 5
SENSITIVE_DB_ENV_KEYS = ('DOLT_CLI_PASSWORD', 'BEADS_DOLT_PASSWORD')
_PR_SET_PDEATHSIG = 1
_AUTH_REFUSAL_MARKERS = (
    'access denied',
    'error 1045',
    'using password',
    'authentication failed',
    'invalid credentials',
)


def db_server_env(env):
    """Return a copy of ``env`` with client password variables stripped.

    ``dolt sql-server`` treats ``DOLT_CLI_PASSWORD`` as a CLI client setting and refuses
    to start without a matching user, and the server never needs the client credential
    variables in its environment.
    """
    cleaned = dict(env)
    for key in SENSITIVE_DB_ENV_KEYS:
        cleaned.pop(key, None)
    return cleaned


def dolt_startup_seconds(cfg):
    """Return the configured Dolt startup wait in seconds (default 30, 1..600)."""
    if not isinstance(cfg, dict) or DOLT_STARTUP_SECONDS_KEY not in cfg:
        return DEFAULT_DOLT_STARTUP_SECONDS
    value = cfg[DOLT_STARTUP_SECONDS_KEY]
    if type(value) is not int or value < MIN_DOLT_STARTUP_SECONDS or value > MAX_DOLT_STARTUP_SECONDS:
        raise ValueError(
            'deployment.private.json key %s must be a whole number of seconds from %d to %d, got %r. '
            'Nothing was changed.' % (
                DOLT_STARTUP_SECONDS_KEY, MIN_DOLT_STARTUP_SECONDS, MAX_DOLT_STARTUP_SECONDS, value
            )
        )
    return value


def dolt_command(root):
    """Return the ``dolt sql-server`` command for ``root``."""
    base = Path(root)
    return [base / 'bin' / 'dolt', 'sql-server', '--config', base / 'server.json']


def _server_json(root):
    """Read ``<root>/server.json`` as a dict, or return ``{}`` if missing or unreadable."""
    try:
        with open(str(Path(root) / 'server.json'), 'r', encoding='utf-8') as stream:
            doc = record_json.loads(stream.read())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def privileges_db_path(root):
    """Return the Dolt ``privileges.db`` path configured by ``<root>/server.json``."""
    base = Path(root)
    doc = _server_json(base)
    cfg_dir = doc.get('cfg_dir')
    if isinstance(cfg_dir, str) and cfg_dir:
        path = Path(cfg_dir)
        if not path.is_absolute():
            path = base / path
        return path / 'privileges.db'
    data_dir = doc.get('data_dir')
    if isinstance(data_dir, str) and data_dir:
        path = Path(data_dir)
        if not path.is_absolute():
            path = base / path
        return path / '.doltcfg' / 'privileges.db'
    return base / 'data' / '.doltcfg' / 'privileges.db'


def socket_path(root):
    """Return the Unix socket path configured by ``<root>/server.json``, or None."""
    base = Path(root)
    doc = _server_json(base)
    listener = doc.get('listener')
    if not isinstance(listener, dict):
        return None
    raw = listener.get('socket')
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path


def cleanup_stale_socket(root):
    """Remove a dead Unix socket left by a killed Dolt process when nothing listens on it.

    Only removes an existing Unix domain socket (never a regular file or symlink) when a
    probe connect fails with ``ECONNREFUSED`` or ``ENOENT``. Returns True if removed.
    """
    af_unix = getattr(socket, 'AF_UNIX', None)
    if af_unix is None:
        return False
    path = socket_path(root)
    if path is None:
        return False
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if not stat.S_ISSOCK(st.st_mode):
        return False
    sock = socket.socket(af_unix, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.2)
        sock.connect(str(path))
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
            try:
                os.unlink(str(path))
                return True
            except OSError:
                return False
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return False


def after_fork():
    """Child-side hook: ask Linux to SIGKILL this child if its parent dies (SIGKILL included)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, 'PR_SET_PDEATHSIG failed')
    if os.getppid() == 1:
        os._exit(1)


def spawn_process(command, root, log_path, env, ensure_dir=None):
    """Start a child in its own process group with PDEATHSIG and a private 0600 log file."""
    target = Path(log_path)
    if ensure_dir is not None:
        ensure_dir(target.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(target), flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'ab', buffering=0) as stream:
        return subprocess.Popen(
            [str(part) for part in command],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=after_fork,
        )


def stop_process(child, deadline):
    """Ask ``child``'s process group to exit with SIGTERM, then SIGKILL after ``deadline``.

    Returns True if SIGKILL was needed, False otherwise.
    """
    if child is None or child.poll() is not None:
        return False
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        return True
    return False


def sql_probe(root, query, cfg, env, password=None, timeout=SQL_PROBE_TIMEOUT):
    """Run one bounded SQL query against ``root``'s loopback Dolt server (Python 3.6 safe)."""
    probe_env = dict(env)
    if password is not None:
        probe_env['DOLT_CLI_PASSWORD'] = password
        probe_env['BEADS_DOLT_PASSWORD'] = password
    command = [
        str(Path(root) / 'bin' / 'dolt'),
        '--host', '127.0.0.1',
        '--port', str(cfg['port']),
        '--no-tls',
        '--user', 'root',
        'sql', '--result-format', 'csv',
    ]
    return subprocess.run(
        command,
        input=query,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf-8',
        env=probe_env,
        cwd=str(root),
        timeout=timeout,
    )


def probe_database(root, cfg, env, timeout=SQL_PROBE_TIMEOUT):
    """Probe loopback Dolt with ``SELECT 1;`` and return ``(ready, detail)``."""
    try:
        done = sql_probe(root, 'SELECT 1;', cfg, env, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    if done.returncode == 0:
        return True, ''
    text = ((done.stderr or '') + '\n' + (done.stdout or '')).strip()
    return False, text or ('dolt sql exited %d' % done.returncode)


def database_ready(root, cfg, env, timeout=SQL_PROBE_TIMEOUT):
    """True only when Dolt answers ``SELECT 1;`` with the deployment's root password."""
    ready, _ = probe_database(root, cfg, env, timeout=timeout)
    return ready


def is_auth_refusal(detail):
    """True when a SQL probe failure indicates Dolt refused the root credentials."""
    lowered = (detail or '').lower()
    for marker in _AUTH_REFUSAL_MARKERS:
        if marker in lowered:
            return True
    return False


def port_listening(port, host='127.0.0.1', timeout=0.5):
    """True when some listener accepts a TCP connection on ``host:port``."""
    try:
        conn = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError:
        return False
    try:
        conn.close()
    except OSError:
        pass
    return True


def _sql_quote_literal(value):
    """Escape backslashes and single quotes for a MySQL/Dolt single-quoted string literal."""
    return value.replace('\\', '\\\\').replace("'", "''")


def bootstrap_database(root, cfg, env):
    """Set the fresh Dolt root password on first start (supervisor only; never restore).

    Wait for the server to answer with the configured password, or with an empty password
    (a fresh data directory), and set ``cfg['password']`` only in the fresh case.
    """
    if database_ready(root, cfg, env):
        return True
    try:
        users = sql_probe(root, 'SELECT User,Host FROM mysql.user;', cfg, env, password='')
    except (subprocess.TimeoutExpired, OSError):
        return False
    if users.returncode != 0:
        return False
    escaped = _sql_quote_literal(cfg['password'])
    changes = [
        "ALTER USER '%s'@'%s' IDENTIFIED BY '%s';" % (
            _sql_quote_literal(row['User']), _sql_quote_literal(row['Host']), escaped
        )
        for row in csv.DictReader(io.StringIO(users.stdout))
        if row.get('User') == 'root'
    ]
    if not changes:
        raise RuntimeError('The fresh Dolt server has no root account to secure')
    changed = sql_probe(root, '\n'.join(changes), cfg, env, password='')
    if changed.returncode != 0:
        # Never include client stdout/stderr: Dolt SQL errors echo the full ALTER USER
        # statement, which would put the root password into the supervisor's error/log.
        raise RuntimeError(
            'Could not set the Dolt root password (dolt sql exited %d)' % changed.returncode
        )
    return database_ready(root, cfg, env)


def _collapse_diagnostic(text, max_chars, keep_tail=False):
    """Sanitize control characters to single spaces and bound output length."""
    cleaned = []
    space = False
    for ch in text:
        code = ord(ch)
        if code < 32 or code == 127:
            if not space and cleaned:
                cleaned.append(' ')
                space = True
        else:
            cleaned.append(ch)
            space = False
    result = ''.join(cleaned).strip()
    if len(result) > max_chars:
        if max_chars <= 3:
            return result[-max_chars:] if keep_tail else result[:max_chars]
        if keep_tail:
            return '...' + result[-(max_chars - 3):].lstrip()
        return result[:max_chars - 3].rstrip() + '...'
    return result


def bounded_diagnostic(log_path=None, detail='', max_bytes=4096, max_chars=500):
    """Read a bounded, single-line diagnostic from ``log_path`` (tail) and/or ``detail``."""
    tail = ''
    if log_path is not None:
        try:
            with open(str(log_path), 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                if size > max_bytes:
                    stream.seek(size - max_bytes)
                else:
                    stream.seek(0)
                raw = stream.read(max_bytes)
            tail = raw.decode('utf-8', 'replace').strip()
        except OSError:
            tail = ''
    probe = (detail or '').strip()
    if tail and probe:
        probe_budget = min(140, max(40, max_chars // 3))
        probe_part = 'last probe: ' + _collapse_diagnostic(probe, probe_budget, keep_tail=False)
        log_budget = max(40, max_chars - len('dolt log: ') - len('; ') - len(probe_part))
        log_part = 'dolt log: ' + _collapse_diagnostic(tail, log_budget, keep_tail=True)
        return log_part + '; ' + probe_part
    if tail:
        return 'dolt log: ' + _collapse_diagnostic(tail, max(20, max_chars - len('dolt log: ')), keep_tail=True)
    if probe:
        return 'last probe: ' + _collapse_diagnostic(probe, max(20, max_chars - len('last probe: ')), keep_tail=False)
    return 'no Dolt output recorded'
