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
import signal
import socket
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


def service_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('schema_version') != 1:
        raise ValueError('Office service configuration must have schema_version 1')
    allowed = {'schema_version', 'http_host', 'http_state', 'cert', 'key',
               'trusted_proxies', 'public_url', 'endpoint_timeout'}
    if set(config) - allowed:
        raise ValueError('Unknown office service setting: ' + ', '.join(sorted(set(config)-allowed)))
    if config.get('http_host', '127.0.0.1') != '127.0.0.1':
        raise ValueError('Office service HTTP listener must use loopback; put TLS at the approved proxy')
    if bool(config.get('cert')) != bool(config.get('key')):
        raise ValueError('cert and key must be supplied together')
    if (not isinstance(config.get('trusted_proxies', []), list) or
            any(not isinstance(value, str) for value in config.get('trusted_proxies', []))):
        raise ValueError('trusted_proxies must be a list of addresses')
    return config


def _after_fork():
    """Ask Linux to kill a child when the supervisor is SIGKILLed."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG failed')
    if os.getppid() == 1:  # parent died between fork and prctl
        os._exit(1)


def _spawn(command, root, log_path, env):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'ab', buffering=0) as stream:
        return subprocess.Popen([str(part) for part in command], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=_after_fork)


def _stop(child, deadline):
    if child is None or child.poll() is not None:
        return
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


def _database_ready(root):
    try:
        admin.sql(root, 'SELECT 1;')
        return True
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False


def _bootstrap_database(root):
    """Finish first-start authentication; retries work after a killed parent."""
    if _database_ready(root):
        return
    users = admin.sql(root, 'SELECT User,Host FROM mysql.user;', password='')
    rows = list(csv.DictReader(io.StringIO(users)))
    hosts = [row['Host'] for row in rows if row.get('User') == 'root']
    if not hosts:
        raise RuntimeError('Fresh Dolt server has no root account')
    password = admin.config(root)['password']
    changes = ["ALTER USER 'root'@'%s' IDENTIFIED BY '%s';" %
               (host.replace("'", "''"), password) for host in hosts]
    admin.sql(root, '\n'.join(changes), password='')
    if not _database_ready(root):
        raise RuntimeError('Dolt root authentication did not become ready')


def prepare(root, db_port):
    if not 1024 <= db_port <= 65535:
        raise ValueError('Database port must be unprivileged')
    vendor = Path(__file__).resolve().parent/'vendor'
    assets = vendor if vendor.is_dir() else None
    if (root/'deployment.private.json').exists():
        cfg = admin.config(root)
        if cfg.get('port') != db_port or not (root/'server.json').is_file():
            raise ValueError('Existing deployment has different or incomplete settings')
        admin.install_binaries(root, asset_dir=assets)
        return
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    admin.install_binaries(root, asset_dir=assets)
    for name in ('data', 'projects', 'backups', 'config', 'dolt-home'):
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
    env = admin.environment(root)
    admin.checked([root/'bin/dolt', 'config', '--global', '--add', 'metrics.disabled', 'true'], env=env)
    admin.checked([root/'bin/dolt', 'config', '--global', '--add', 'user.name', 'Orchestra service'], env=env)
    admin.checked([root/'bin/dolt', 'config', '--global', '--add', 'user.email', 'orchestra@localhost'], env=env)
    admin.checked([root/'bin/bd', 'metrics', 'off'], env=env)


def health(root, port):
    version = report(Path(__file__).resolve().parent)['version']
    db = _database_ready(root)
    try:
        with urlopen('http://127.0.0.1:%d/healthz' % port, timeout=2) as response:
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


def run(root, logs, config, port, stop_seconds):
    if os.name != 'posix' or not sys.platform.startswith('linux'):
        raise ValueError('Foreground supervision requires Linux')
    if not 1024 <= port <= 65535 or not 1 <= stop_seconds <= 30:
        raise ValueError('Use an unprivileged HTTP port and a 1-30 second stop deadline')
    if not (root/'server.json').is_file() or not (root/'deployment.private.json').is_file():
        raise ValueError('Prepare this runtime before starting it')
    settings = service_config(config)
    logs = Path(logs).expanduser().resolve()
    logs.mkdir(parents=True, exist_ok=True)
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
        try:
            env = admin.environment(root)
            db_env = env.copy()
            db_env.pop('DOLT_CLI_PASSWORD', None)
            db_env.pop('BEADS_DOLT_PASSWORD', None)
            db = _spawn([root/'bin/dolt', 'sql-server', '--config', root/'server.json'],
                        root, logs/'dolt.log', db_env)
            until = time.monotonic()+30
            while time.monotonic() < until and not stop:
                if db.poll() is not None:
                    raise RuntimeError('Dolt exited during startup; inspect dolt.log')
                try:
                    _bootstrap_database(root)
                    break
                except (OSError, subprocess.CalledProcessError):
                    time.sleep(.2)
            else:
                raise RuntimeError('Dolt did not become ready within 30 seconds')
            command = [sys.executable, Path(__file__).with_name('http_service.py'),
                       '--state', settings.get('http_state', str(root/'http-state.json')),
                       '--host', '127.0.0.1', '--port', port,
                       '--backend', 'endpoint', '--endpoint-python', sys.executable,
                       '--endpoint', Path(__file__).with_name('endpoint.py'), '--root', root]
            for arg, key in (('--cert','cert'), ('--key','key'), ('--public-url','public_url')):
                if settings.get(key): command.extend([arg, settings[key]])
            for proxy in settings.get('trusted_proxies', []):
                command.extend(['--trusted-proxy', proxy])
            if settings.get('endpoint_timeout'):
                command.extend(['--endpoint-timeout', settings['endpoint_timeout']])
            web = _spawn(command, root, logs/'http.log', env)
            while not stop:
                if db.poll() is not None or web.poll() is not None:
                    raise RuntimeError('A supervised child exited; inspect service logs')
                time.sleep(.2)
        finally:
            deadline = time.monotonic()+stop_seconds
            _stop(web, deadline)
            _stop(db, deadline)
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','run','health'))
    parser.add_argument('--root', required=True)
    parser.add_argument('--config', help='private office service JSON (run)')
    parser.add_argument('--logs', help='log directory (run)')
    parser.add_argument('--port', type=int, default=10000, help='HTTP loopback port')
    parser.add_argument('--db-port', type=int, default=13307, help='Dolt loopback port (prepare)')
    parser.add_argument('--stop-seconds', type=int, default=5)
    args = parser.parse_args(argv)
    root = private_root(args.root)
    try:
        if args.action == 'prepare':
            prepare(root, args.db_port)
            print('Prepared private runtime %s' % root)
        elif args.action == 'health':
            line, code = health(root, args.port)
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
