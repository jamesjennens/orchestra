#!/usr/bin/env python3
"""Foreground supervisor for an unprivileged Orchestra deployment on Linux.

The external scheduler owns this process. No systemd user manager or root access is
needed. Private state lives under --root; this file contains no deployment settings.
"""
import argparse
import contextlib
import csv
import ctypes
import fcntl
import io
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import admin
from version import report


def private_root(raw):
    requested = Path(raw).expanduser()
    if not requested.is_absolute() or requested.is_symlink():
        raise ValueError('Use a real absolute runtime directory, not a symlink')
    root = requested.resolve()
    if root == Path('/'):
        raise ValueError('Use an explicit non-root runtime directory')
    return root


#: The three ways the web interface is served (kittrial-5bb.163). ``loopback``: plain HTTP on
#: this host only, reached through a proxy or a tunnel. ``https``: a certificate and key are
#: given, on any address. ``plain-http-on-network``: no certificate, on an address other
#: hosts can reach; only with the explicit setting below, and with a warning at every start.
LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost')
WILDCARD_HOSTS = ('0.0.0.0', '::')
HOST = re.compile(r'[A-Za-z0-9:]([A-Za-z0-9.:-]{0,251}[A-Za-z0-9:])?')
PLAINTEXT_SETTING = 'allow_plaintext_on_network'
PLAINTEXT_WARNING = ('WARNING: serving plain HTTP on %s:%d. Passwords and session cookies cross the network '
                     'unencrypted. Use cert and key for HTTPS.')


def service_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('schema_version') != 1:
        raise ValueError('Office service configuration must have schema_version 1')
    allowed = {'schema_version', 'http_host', 'http_state', 'cert', 'key',
               'trusted_proxies', 'public_url', 'endpoint_timeout', 'connections_per_address', PLAINTEXT_SETTING}
    if set(config) - allowed:
        raise ValueError('Unknown office service setting: ' + ', '.join(sorted(set(config)-allowed)))
    if bool(config.get('cert')) != bool(config.get('key')):
        raise ValueError('cert and key must be supplied together')
    host = config.get('http_host', '127.0.0.1')
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise ValueError('http_host must be a host name or an address')
    plain = config.get(PLAINTEXT_SETTING, False)
    if not isinstance(plain, bool):
        raise ValueError(PLAINTEXT_SETTING + ' must be true or false')
    # Loopback stays the default. Off loopback the listener needs a certificate, or the
    # operator's explicit word that plain HTTP on the network is meant.
    if plain and host in LOOPBACK_HOSTS:
        raise ValueError('%s is set but http_host is loopback: remove the setting, or name the address to serve on'
                         % PLAINTEXT_SETTING)
    if plain and config.get('cert'):
        raise ValueError('%s is set together with cert and key: remove one of the two; with a certificate the '
                         'service serves HTTPS' % PLAINTEXT_SETTING)
    if host not in LOOPBACK_HOSTS and not config.get('cert') and not plain:
        raise ValueError('http_host %s is not loopback and no cert and key are given. Give both for HTTPS, or set '
                         '%s to true to serve plain HTTP on the network (passwords and session cookies then cross '
                         'it unencrypted)' % (host, PLAINTEXT_SETTING))
    if host in WILDCARD_HOSTS and not config.get('public_url'):
        raise ValueError('http_host %s listens on every address, so the service cannot name itself: set public_url '
                         'to the address people use' % host)
    if (not isinstance(config.get('trusted_proxies', []), list) or
            any(not isinstance(value, str) for value in config.get('trusted_proxies', []))):
        raise ValueError('trusted_proxies must be a list of addresses')
    per_address = config.get('connections_per_address', 0)
    if isinstance(per_address, bool) or not isinstance(per_address, int) or per_address < 0:
        raise ValueError('connections_per_address must be a whole number: how many connections one client address '
                         'may have open at once, or 0 for no limit per address')
    return config


def _after_fork():
    """Ask Linux to kill a child when the supervisor is SIGKILLed."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG failed')
    if os.getppid() == 1:  # parent died between fork and prctl
        os._exit(1)


def log_directory(raw):
    """Create the log directory, tightening only a new directory this account owns.

    A scheduler may pass a log directory owned by another account (for example a
    supervisor that collects logs as a different user or group). Chmod there either
    fails with EPERM or locks that account out, so an existing directory is left
    exactly as it is; the 0600 log files opened below carry the confidentiality.
    """
    directory = Path(raw)
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if existed:
        return
    try:
        if directory.stat().st_uid == os.geteuid():
            directory.chmod(0o700)
    except OSError:
        pass


def _spawn(command, root, log_path, env):
    log_directory(log_path.parent)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'ab', buffering=0) as stream:
        return subprocess.Popen([str(part) for part in command], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=_after_fork)


def _stop(child, deadline):
    if child is None or child.poll() is not None:
        return False
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=max(0, deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        return True
    return False


def _sql_probe(root, query, password=None):
    """Bound each readiness query so shutdown cannot hang in the SQL client."""
    cfg = admin.config(root)
    env = admin.environment(root)
    if password is not None:
        env['DOLT_CLI_PASSWORD'] = password
    return subprocess.run(
        [str(root/'bin/dolt'), '--host', '127.0.0.1', '--port', str(cfg['port']),
         '--no-tls', '--user', 'root', 'sql', '--result-format', 'csv'],
        input=query, text=True, encoding='utf-8', capture_output=True,
        check=True, timeout=5, env=env, cwd=root).stdout


def _database_ready(root):
    try:
        _sql_probe(root, 'SELECT 1;')
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return False


def _bootstrap_database(root):
    """Finish first-start authentication; retries work after a killed parent."""
    if _database_ready(root):
        return
    users = _sql_probe(root, 'SELECT User,Host FROM mysql.user;', password='')
    rows = list(csv.DictReader(io.StringIO(users)))
    hosts = [row['Host'] for row in rows if row.get('User') == 'root']
    if not hosts:
        raise RuntimeError('Fresh Dolt server has no root account')
    password = admin.config(root)['password']
    changes = ["ALTER USER 'root'@'%s' IDENTIFIED BY '%s';" %
               (host.replace("'", "''"), password) for host in hosts]
    _sql_probe(root, '\n'.join(changes), password='')
    if not _database_ready(root):
        raise RuntimeError('Dolt root authentication did not become ready')


def _install_binaries(root, assets):
    """Install the pinned binaries, turning bootstrap's ``SystemExit`` into a failure.

    ``bootstrap.install`` refuses a non-Linux host and a binary/receipt mismatch by
    raising ``SystemExit``. That is not in ``main``'s handler, so prepare's own
    contract ("a failed step fails prepare, with the reason") would depend on the
    interpreter instead of the command. Raise an ordinary error carrying the same
    sentence, which ``main`` prints and turns into exit code 1.
    """
    try:
        admin.install_binaries(root, asset_dir=assets)
    except SystemExit as error:
        raise RuntimeError(str(error) or 'Installing the pinned binaries failed') from None


def _verify_bd_starts(root):
    """Run ``bd --version`` so a bundled bd that cannot start fails prepare here.

    ``install_binaries`` verifies the archive and binary digests but never runs the
    binary. A dynamically linked bd can be digest-correct and still be refused by the
    loader on an older host (the rehearsal's pinned bd needs glibc 2.34), and the old
    prepare only found out later, mid-command. Running it now reports the loader's own
    sentence at prepare time. Only bd is checked here: it is the binary the rehearsal
    showed can be digest-correct yet unstartable, and the pinned-binary start check at
    build and install time is kittrial-5bb.161's separate change.
    """
    path = root/'bin/bd'
    try:
        admin.checked([path, '--version'], env=admin.environment(root))
    except (OSError, subprocess.CalledProcessError) as error:
        detail = (getattr(error, 'stderr', '') or str(error)).strip()
        raise RuntimeError('%s cannot start: %s' % (path, detail or error)) from None


def prepare(root, db_port):
    if not 1024 <= db_port <= 65535:
        raise ValueError('Database port must be unprivileged')
    vendor = Path(__file__).resolve().parent/'vendor'
    assets = vendor if vendor.is_dir() else None
    if (root/'deployment.private.json').exists():
        cfg = admin.config(root)
        if cfg.get('port') != db_port or not (root/'server.json').is_file():
            raise ValueError('Existing deployment has different or incomplete settings')
        _install_binaries(root, assets)
        _verify_bd_starts(root)
        return
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    _install_binaries(root, assets)
    for name in ('data', 'projects', 'backups', 'config', 'dolt-home', 'home'):
        (root/name).mkdir(exist_ok=True)
    import secrets
    cfg = {'schema': 1, 'port': db_port, 'unit': 'office-foreground',
           'password': secrets.token_hex(24)}
    server = {'log_level': 'warning',
              'behavior': {'autocommit': True, 'auto_gc_behavior': {'enable': False}},
              'listener': {'host': '127.0.0.1', 'port': db_port,
                           'socket': str(root/'mysql.sock')},
              'data_dir': str(root/'data'), 'cfg_dir': str(root/'data/.doltcfg'),
              'metrics': {'port': -1}}
    admin.atomic_private_write(root/'server.json', json.dumps(server, indent=2)+'\n')
    admin.atomic_private_write(root/'deployment.private.json', json.dumps(cfg, indent=2)+'\n')
    # After the private configuration exists: bd's runtime environment is scoped with
    # HOME under the runtime, which reads the stored deployment password.
    _verify_bd_starts(root)
    env = admin.environment(root)
    admin.checked([root/'bin/dolt', 'config', '--global', '--add', 'metrics.disabled', 'true'], env=env)
    admin.checked([root/'bin/dolt', 'config', '--global', '--add', 'user.name', 'Orchestra service'], env=env)
    admin.checked([root/'bin/dolt', 'config', '--global', '--add', 'user.email', 'orchestra@localhost'], env=env)
    admin.checked([root/'bin/bd', 'metrics', 'off'], env=env)


def listener(settings, port):
    """How the web interface is served with ``settings`` (the private office JSON, or None): one of
    the three shapes, the address it is bound to, the address to ask on this host, and its URL."""
    settings = settings or {}
    host = settings.get('http_host', '127.0.0.1')
    secure = bool(settings.get('cert'))
    shape = 'https' if secure else ('loopback' if host in LOOPBACK_HOSTS else 'plain-http-on-network')
    # A wildcard is asked on loopback; any other address is asked as it is bound.
    asked = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    bracket = lambda name: '[%s]' % name if ':' in name else name
    scheme = 'https' if secure else 'http'
    return {'shape': shape, 'host': host, 'port': port,
            'probe': '%s://%s:%d/healthz' % (scheme, bracket(asked), port),
            # Named for the people who use it only off loopback: a loopback listener is reached
            # through a proxy or a tunnel, whose address the service does not know.
            'public_url': settings.get('public_url') or (
                None if host in LOOPBACK_HOSTS else '%s://%s:%d' % (scheme, bracket(host), port))}


def health(root, port, settings=None):
    version = report(Path(__file__).resolve().parent)['version']
    db = _database_ready(root)
    served = listener(settings, port)
    try:
        if served['probe'].startswith('https:'):
            # The service asking its own listener whether it answers, on this host. The
            # certificate is not verified here: a self-signed one must work, and this
            # probe sends nothing and trusts nothing it reads beyond "200".
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            response = urlopen(served['probe'], timeout=2, context=context)
        else:
            response = urlopen(served['probe'], timeout=2)
        with response:
            web = response.status == 200
    except (OSError, ValueError):
        web = False
    try:
        status = admin.read_backup_status(root)
        backup = status['status']
        if admin.require_complete_problems(root, status):
            backup = 'incomplete'
        backup_time = status.get('generated_at', 'unknown')
    except (OSError, ValueError, KeyError):
        backup, backup_time = 'unknown', 'unknown'
    ok = db and web and backup == 'complete'
    return ('version=%s db=%s web=%s backup=%s backup_at=%s' %
            (version, 'up' if db else 'down', 'up' if web else 'down',
             backup, backup_time)), 0 if ok else 1


def web_command(settings, root, port, release_python, release_script):
    """The command line of the web service for ``settings``: host, certificate, plain-HTTP word, URL."""
    served = listener(settings, port)
    command = [release_python, release_script.with_name('http_service.py'),
               '--state', settings.get('http_state', str(root/'http-state.json')),
               '--host', served['host'], '--port', port,
               '--backend', 'endpoint', '--endpoint-python', release_python,
               '--endpoint', release_script.with_name('endpoint.py'), '--root', root]
    for arg, key in (('--cert','cert'), ('--key','key')):
        if settings.get(key): command.extend([arg, settings[key]])
    if served['public_url']:
        command.extend(['--public-url', served['public_url']])
    if served['shape'] == 'plain-http-on-network':
        command.append('--allow-plaintext-on-network')
    if 'connections_per_address' in settings:
        command.extend(['--connections-per-address', settings['connections_per_address']])
    return command


def run(root, logs, config, port, stop_seconds):
    if os.name != 'posix' or not sys.platform.startswith('linux'):
        raise ValueError('Foreground supervision requires Linux')
    if not 1024 <= port <= 65535 or not 1 <= stop_seconds <= 30:
        raise ValueError('Use an unprivileged HTTP port and a 1-30 second stop deadline')
    if not (root/'server.json').is_file() or not (root/'deployment.private.json').is_file():
        raise ValueError('Prepare this runtime before starting it')
    settings = service_config(config)
    logs = Path(logs).expanduser().resolve()
    log_directory(logs)
    lock_fd = os.open(root/'office-service.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Office service is already running for this runtime') from None
        stop = False
        def request_stop(_signum, _frame):
            nonlocal stop
            stop = True
        old_term = signal.signal(signal.SIGTERM, request_stop)
        old_int = signal.signal(signal.SIGINT, request_stop)
        db = web = None
        forced = False
        try:
            env = admin.environment(root)
            db_env = env.copy()
            db_env.pop('DOLT_CLI_PASSWORD', None)
            db_env.pop('BEADS_DOLT_PASSWORD', None)
            db = _spawn([root/'bin/dolt', 'sql-server', '--config', root/'server.json'],
                        root, logs/'dolt.log', db_env)
            until = time.monotonic()+30
            ready = False
            while time.monotonic() < until and not stop:
                if db.poll() is not None:
                    raise RuntimeError('Dolt exited during startup; inspect dolt.log')
                try:
                    _bootstrap_database(root)
                    ready = True
                    break
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    time.sleep(.2)
            if not stop and not ready:
                raise RuntimeError('Dolt did not become ready within 30 seconds')
            release_script = Path(__file__).resolve()
            release_python = os.path.realpath(sys.executable)
            command = web_command(settings, root, port, release_python, release_script)
            if listener(settings, port)['shape'] == 'plain-http-on-network':
                print(PLAINTEXT_WARNING % (settings['http_host'], port), file=sys.stderr, flush=True)
            for proxy in settings.get('trusted_proxies', []):
                command.extend(['--trusted-proxy', proxy])
            if settings.get('endpoint_timeout'):
                command.extend(['--endpoint-timeout', settings['endpoint_timeout']])
            if not stop:
                web = _spawn(command, root, logs/'http.log', env)
                while not stop:
                    if db.poll() is not None or web.poll() is not None:
                        raise RuntimeError('A supervised child exited; inspect service logs')
                    time.sleep(.2)
        finally:
            deadline = time.monotonic()+stop_seconds
            web_forced = _stop(web, time.monotonic()+stop_seconds/2)
            db_forced = _stop(db, deadline)
            forced = web_forced or db_forced
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        if forced:
            raise RuntimeError('A supervised child required SIGKILL during shutdown')
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','run','health'))
    parser.add_argument('--root', required=True)
    parser.add_argument('--config', help='private office service JSON (run; health, to ask the listener it configures)')
    parser.add_argument('--logs', help='log directory (run)')
    parser.add_argument('--port', type=int, default=10000, help='HTTP port (on loopback unless the configuration says otherwise)')
    parser.add_argument('--db-port', type=int, default=13307, help='Dolt loopback port (prepare)')
    parser.add_argument('--stop-seconds', type=int, default=5)
    args = parser.parse_args(argv)
    root = private_root(args.root)
    try:
        if args.action == 'prepare':
            prepare(root, args.db_port)
            print('Prepared private runtime %s' % root)
        elif args.action == 'health':
            line, code = health(root, args.port, service_config(args.config) if args.config else None)
            print(line)
            return code
        else:
            if not args.config or not args.logs:
                parser.error('run requires --config and --logs')
            run(root, args.logs, args.config, args.port, args.stop_seconds)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('office-service: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
