#!/usr/bin/env python3
"""Foreground supervisor for an unprivileged Orchestra deployment on Linux.

The external scheduler owns this process. No systemd user manager or root access is
needed. Private state lives under --root; this file contains no deployment settings.
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('office_service.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer; on an office installation that is the bundled interpreter, INSTALL_ROOT/current/python-runtime/....\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
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
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import admin
import record_json
from version import report
from http_auth import (session_limit_seconds, validate_agent_credential_days,
                       validate_password_min_length)


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
#: A lone ``0`` is the system's ``0.0.0.0`` (kittrial-5bb.168 item 5): it binds every address just
#: as the long forms do, so it needs ``public_url`` too.
WILDCARD_HOSTS = ('0.0.0.0', '0', '::')
HOST = re.compile(r'[A-Za-z0-9:]([A-Za-z0-9.:-]{0,251}[A-Za-z0-9:])?')
#: A host with a port written into it: ``name:1234``, or a bracketed IPv6 literal with a port. A
#: bare IPv6 literal (``::``, ``fd00::5``) has two or more colons and no port, so it does not match.
PORT_IN_HOST = re.compile(r'^(?:\[[^\]]+\]|[^:]+):[0-9]{1,5}$')
PLAINTEXT_SETTING = 'allow_plaintext_on_network'
#: Off by default (kittrial-5bb.168 item 6): turning it on asks every browser that reached the
#: service over HTTPS to use HTTPS only for this host, which a self-signed first install must not
#: force, and a proxy in front already may. The operator's word turns it on for HTTPS responses.
HSTS_SETTING = 'strict_transport_security'
PLAINTEXT_WARNING = ('WARNING: serving plain HTTP on %s:%d. Passwords and session cookies cross the network '
                     'unencrypted. Use cert and key for HTTPS.')


def service_config(path):
    # The file itself is named for every way reading it can fail (kittrial-5bb.168 item 3): a
    # plain JSON error named neither the file nor that the file was the problem.
    try:
        text = admin.read_regular_text(path)
    except OSError as error:
        raise ValueError('Cannot read office service configuration %s: %s'
                         % (path, error.strerror or error)) from None
    except ValueError as error:
        if isinstance(error, UnicodeError):
            raise
        raise ValueError('Cannot read office service configuration %s: %s'
                         % (path, error)) from None
    try:
        config = json.loads(text)
    except ValueError:
        raise ValueError('Office service configuration %s is not JSON: give valid JSON, or the path of the '
                         'configuration file' % path) from None
    if not isinstance(config, dict):
        raise ValueError('Office service configuration %s must be a JSON object, not %s'
                         % (path, type(config).__name__))
    if config.get('schema_version') != 1:
        raise ValueError('Office service configuration must have schema_version 1')
    allowed = {'schema_version', 'http_host', 'http_state', 'cert', 'key',
               'trusted_proxies', 'public_url', 'endpoint_timeout', 'connections_per_address', 'logins_per_address',
               'approval_by_another_party', 'agent_credential_days', 'password_min_length',
               'session_idle_minutes', 'session_absolute_hours', PLAINTEXT_SETTING, HSTS_SETTING}
    if set(config) - allowed:
        raise ValueError('Unknown office service setting: ' + ', '.join(sorted(set(config)-allowed)))
    for setting in ('cert', 'key'):
        value = config.get(setting)
        if value is not None and not isinstance(value, str):
            raise ValueError('%s must be the path of a file, as a string, not %s'
                             % (setting, type(value).__name__))
    if bool(config.get('cert')) != bool(config.get('key')):
        raise ValueError('cert and key must be supplied together')
    host = config.get('http_host', '127.0.0.1')
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise ValueError('http_host must be a host name or an address')
    if PORT_IN_HOST.fullmatch(host):
        raise ValueError('http_host must not carry a port: give the address alone and the port in --port (got %s)' % host)
    public_url = config.get('public_url')
    if public_url is not None:
        # kittrial-5bb.360: the address is copied into the set-up commands people paste into a
        # shell, so it must be a plain http(s) address: one rule (server_address.py), one sentence
        # naming the setting, this file and the first character it refuses.
        from server_address import public_url_problem
        problem = public_url_problem(public_url, 'the office configuration (%s)' % os.path.basename(str(path)))
        if problem:
            raise ValueError(problem)
    if not isinstance(config.get('approval_by_another_party', False), bool):
        raise ValueError('approval_by_another_party must be true or false')
    if 'agent_credential_days' in config:
        validate_agent_credential_days(config['agent_credential_days'])
    # The installation's password minimum (kittrial-5bb.363), refused by the same helper and
    # the same sentence as the web service's own flag. An omitted key keeps 1 (any non-empty
    # password); the 1024 maximum is a bound on work per request, not a policy.
    if 'password_min_length' in config:
        validate_password_min_length(config['password_min_length'])
    # The session idle and absolute limits (kittrial-5bb.330), refused by the same helper and
    # the same one sentence as the web service's own flags. An omitted key keeps 30 min / 12 h.
    session_limit_seconds(config.get('session_idle_minutes'), config.get('session_absolute_hours'))
    plain = config.get(PLAINTEXT_SETTING, False)
    if not isinstance(plain, bool):
        raise ValueError(PLAINTEXT_SETTING + ' must be true or false')
    if not isinstance(config.get(HSTS_SETTING, False), bool):
        raise ValueError(HSTS_SETTING + ' must be true or false')
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
    # How much of the web service one client address may take; the totals are the service's own.
    for setting, total, of_what in (('connections_per_address', 200, 'connections one client address may have open at once'),
                                    ('logins_per_address', 16, 'log-ins one client address may have in flight at once')):
        if setting in config:
            value = config[setting]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= total:
                raise ValueError('%s must be a whole number from 1 to %d: how many of the %d %s'
                                 % (setting, total, total, of_what))
    return config


#: The PEM markers that say what a file is without decoding it.
_PEM_CERTIFICATE = '-----BEGIN CERTIFICATE-----'
_PEM_KEYS = ('-----BEGIN PRIVATE KEY-----', '-----BEGIN RSA PRIVATE KEY-----',
             '-----BEGIN EC PRIVATE KEY-----', '-----BEGIN ENCRYPTED PRIVATE KEY-----')


def _readable_file(path):
    """Why ``path`` cannot be read, or None: 'missing', 'directory', 'notafile' or 'unreadable'."""
    try:
        if not os.path.exists(path):
            return 'missing'
        if os.path.isdir(path):
            return 'directory'
        # A FIFO, device or socket is opened before it is read, which can block a start forever.
        if not stat.S_ISREG(os.stat(path).st_mode):
            return 'notafile'
        with open(path, 'rb') as stream:
            stream.read(1)
    except OSError:
        return 'unreadable'
    return None


def _pem_kind(path):
    """'certificate', 'key', 'encrypted-key' or None, from the first bytes of the file only."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as stream:
            head = stream.read(4096)
    except OSError:
        return None
    if '-----BEGIN ENCRYPTED PRIVATE KEY-----' in head or 'Proc-Type: 4,ENCRYPTED' in head:
        return 'encrypted-key'
    if _PEM_CERTIFICATE in head:
        return 'certificate'
    if any(marker in head for marker in _PEM_KEYS):
        return 'key'
    return None


def certificate_path(value, root):
    """The certificate or key file a setting names, as the WEB CHILD will open it.

    A relative path is relative to the runtime root, because the child is spawned with
    ``cwd=root``; resolving it any other way makes the supervisor check one file while the child
    serves another (kittrial-5bb.168 review 1). The supervisor hands the child this absolute path
    so both read the same file.
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)
    return os.path.normpath(os.path.join(str(root), str(path)))


def certificate_preflight(settings, root):
    """Load the certificate and key before anything is started (kittrial-5bb.168 item 1).

    The pair used to be loaded only inside the web child, so the supervisor said only "A
    supervised child exited" and ``http.log`` ended in a traceback. Here every way the pair can
    be unusable raises one sentence that names the file and what is wrong: the caller prints it
    and starts nothing. The pair is loaded with an empty password, so an encrypted key is
    refused here instead of asking the terminal for a passphrase. No key text is ever printed.

    Returns the ``(cert, key)`` the child must be given - the files that were checked, with a
    relative ``cert``/``key`` resolved against ``root`` - or ``None`` when no certificate is
    configured.
    """
    cert, key = settings.get('cert'), settings.get('key')
    if not cert:
        return None
    cert, key = certificate_path(cert, root), certificate_path(key, root)
    for name, path in (('certificate', cert), ('key', key)):
        state = _readable_file(path)
        if state == 'missing':
            raise ValueError('The %s file %s does not exist. Give the path of the file that was installed'
                             % (name, path))
        if state == 'directory':
            raise ValueError('The %s file %s is a directory, not a file' % (name, path))
        if state == 'notafile':
            raise ValueError('The %s file %s is not a regular file' % (name, path))
        if state == 'unreadable':
            raise ValueError('The %s file %s cannot be read (permission denied). Make it readable by the '
                             'service account' % (name, path))
    cert_kind, key_kind = _pem_kind(cert), _pem_kind(key)
    if cert_kind in ('key', 'encrypted-key') and key_kind == 'certificate':
        raise ValueError('The certificate file %s and the key file %s look swapped: give the certificate in cert '
                         'and the private key in key' % (cert, key))
    if cert_kind != 'certificate':
        raise ValueError('The certificate file %s is not a PEM certificate: it must hold a %s block'
                         % (cert, _PEM_CERTIFICATE))
    if key_kind == 'encrypted-key':
        raise ValueError('The key file %s needs a passphrase, and the office service cannot ask for one. Give an '
                         'unencrypted key (openssl rsa -in %s -out <unencrypted.key>)' % (key, key))
    if key_kind != 'key':
        raise ValueError('The key file %s is not a PEM private key: it must hold a PRIVATE KEY block' % key)
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key, password='')
    except ssl.SSLError as failed:
        if getattr(failed, 'errno', None) == 116 or 'KEY_VALUES_MISMATCH' in str(failed):
            raise ValueError('The key file %s does not match the certificate file %s: they are not a pair'
                             % (key, cert)) from None
        raise ValueError('The certificate file %s and the key file %s are each PEM, but not a pair this service '
                         'can serve' % (cert, key)) from None
    except OSError:
        raise ValueError('The certificate file %s and the key file %s cannot be loaded: a passphrase, or a form '
                         'of PEM this service cannot use' % (cert, key)) from None
    return cert, key


def certificate_window(path):
    """(notBefore, notAfter) as epoch seconds, or None when this interpreter cannot read them.

    ``ssl._ssl._test_decode_cert`` is the decoder CPython itself uses to fill ``getpeercert()``;
    when this interpreter lacks it, ``cryptography`` is tried; when neither is available the
    caller says the warning is skipped rather than guessing the dates.
    """
    decoder = getattr(getattr(ssl, '_ssl', None), '_test_decode_cert', None)
    if decoder is not None:
        try:
            decoded = decoder(str(path))
            return (ssl.cert_time_to_seconds(decoded['notBefore']),
                    ssl.cert_time_to_seconds(decoded['notAfter']))
        except (OSError, ValueError, KeyError, ssl.SSLError):
            return None
    try:
        from datetime import timezone
        from cryptography import x509
        with open(path, 'rb') as stream:
            parsed = x509.load_pem_x509_certificate(stream.read())
        return (int(parsed.not_valid_before.replace(tzinfo=timezone.utc).timestamp()),
                int(parsed.not_valid_after.replace(tzinfo=timezone.utc).timestamp()))
    except (ImportError, OSError, ValueError, AttributeError):
        return None


def start_warnings(settings):
    """The warning lines a start prints: a shared-readable key, and an out-of-date certificate.

    The key's mode is a WARNING, not a refusal (kittrial-5bb.168 item 4): a first install can have
    a root-owned key readable by the service account's group, and refusing would break it. The
    reason is in docs/OFFICE_SERVICE.md. An expired or not-yet-valid certificate is a WARNING too;
    when the interpreter cannot read the certificate's validity the warning is skipped and that
    is said, rather than guessing.
    """
    lines = []
    key = settings.get('key')
    if key:
        try:
            mode = os.stat(key).st_mode & 0o777
        except OSError:
            mode = None
        if mode is not None and mode & 0o077:
            lines.append('WARNING: the key file %s is readable by %s (mode %04o). A first install may have a '
                         'root-owned key readable by the service account group; tighten it with chmod 600 %s.'
                         % (key, 'everyone' if mode & 0o007 else 'the group', mode, key))
    cert = settings.get('cert')
    if cert:
        window = certificate_window(cert)
        now = time.time()
        if window is None:
            lines.append('NOTE: the validity of the certificate %s cannot be read on this interpreter, so no '
                         'expiry warning is given.' % cert)
        elif window[1] < now:
            lines.append('WARNING: the certificate %s expired on %s. Replace it before it is used further.'
                         % (cert, time.strftime('%Y-%m-%d', time.gmtime(window[1]))))
        elif window[0] > now:
            lines.append('WARNING: the certificate %s is not valid until %s.'
                         % (cert, time.strftime('%Y-%m-%d', time.gmtime(window[0]))))
    return lines


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
    asked = {'0.0.0.0': '127.0.0.1', '0': '127.0.0.1', '::': '::1'}.get(host, host)
    bracket = lambda name: '[%s]' % name if ':' in name else name
    scheme = 'https' if secure else 'http'
    return {'shape': shape, 'host': host, 'port': port,
            'probe': '%s://%s:%d/healthz' % (scheme, bracket(asked), port),
            # Named for the people who use it only off loopback: a loopback listener is reached
            # through a proxy or a tunnel, whose address the service does not know.
            'public_url': settings.get('public_url') or (
                None if host in LOOPBACK_HOSTS else '%s://%s:%d' % (scheme, bracket(host), port))}


def _bounded(value, limit=160):
    """One line, short enough for the health line, whatever the probe said."""
    return ' '.join(str(value).split())[:limit]


def _web_probe(served, expected):
    """Whether the listener answers as this service, and why not when it does not.

    ``/healthz`` carries this service's own mark (kittrial-5bb.168 item 8): its name, version and
    build id. A plain web server, or another Orchestra service, answering 200 on the port does not
    carry it, and reads ``web=down`` with the reason. The returned reason is bounded.
    """
    try:
        if served['probe'].startswith('https:'):
            # The service asking its own listener whether it answers, on this host. The
            # certificate is not verified here: a self-signed one must work, and this
            # probe sends nothing and trusts nothing it reads beyond the mark.
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            response = urlopen(served['probe'], timeout=2, context=context)
        else:
            response = urlopen(served['probe'], timeout=2)
        with response:
            status = response.status
            raw = response.read(2000) if status == 200 else b''
        if status != 200:
            return False, 'the health address answered HTTP %s, not 200' % status
        try:
            # Through the kit's guard, not bare json.loads: the answer can come from WHATEVER
            # holds the port (that is what the mark below is for), and a deeply nested one must
            # read as "not this service" rather than raising RecursionError (kittrial-5bb.168
            # review item 2). A NestingError IS a ValueError, as caught here.
            mark = record_json.loads(raw.decode('utf-8', 'replace')) if raw else {}
        except ValueError:
            mark = None
        if isinstance(mark, dict) and mark.get('service') == 'orchestra-office':
            if (mark.get('version'), mark.get('build_id')) == expected:
                return True, ''
            return False, ('another Orchestra service holds the port (it reports version=%s build_id=%s)'
                           % (_bounded(mark.get('version'), 40), _bounded(mark.get('build_id'), 40)))
        return False, 'another program holds the port; its /healthz answer is not this service\'s'
    except (OSError, ValueError) as failed:
        return False, _bounded(getattr(failed, 'reason', None) or failed)


def health(root, port, settings=None):
    info = report(Path(__file__).resolve().parent)
    db = _database_ready(root)
    served = listener(settings, port)
    web, web_reason = _web_probe(served, (info['version'], info['build_id']))
    backup_reason = ''
    try:
        status = admin.read_backup_status(root)
        backup = status['status']
        if admin.require_complete_problems(root, status):
            backup = 'incomplete'
        backup_time = status.get('generated_at', 'unknown')
    except (OSError, ValueError, KeyError) as failed:
        backup, backup_time = 'unknown', 'unknown'
        backup_reason = _bounded(failed)
    line = ('version=%s db=%s web=%s backup=%s backup_at=%s' %
            (info['version'], 'up' if db else 'down', 'up' if web else 'down', backup, backup_time))
    reasons = []
    if not web and web_reason:
        reasons.append('web: %s' % web_reason)
    if backup == 'unknown' and backup_reason:
        reasons.append('backup: %s' % backup_reason)
    if reasons:
        line += ' reason=' + '; '.join(reasons)
    ok = db and web and backup == 'complete'
    return line, 0 if ok else 1


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
    if 'logins_per_address' in settings:
        command.extend(['--logins-per-address', settings['logins_per_address']])
    if 'agent_credential_days' in settings:
        command.extend(['--agent-credential-days', validate_agent_credential_days(settings['agent_credential_days'])])
    if 'password_min_length' in settings:
        command.extend(['--password-min-length', validate_password_min_length(settings['password_min_length'])])
    if 'session_idle_minutes' in settings or 'session_absolute_hours' in settings:
        # Validated once as a pair, so the pair handed on is the pair that was checked: the
        # absolute limit is never shorter than the idle window (kittrial-5bb.330).
        idle, absolute = session_limit_seconds(settings.get('session_idle_minutes'),
                                               settings.get('session_absolute_hours'))
        command.extend(['--session-idle-minutes', idle // 60, '--session-absolute-hours', absolute // 3600])
    if settings.get('approval_by_another_party') is True:
        command.append('--approval-by-another-party')
    if settings.get(HSTS_SETTING) is True:
        command.append('--strict-transport-security')
    return command


#: The web service's exit statuses for a start that failed (``http_service.EXIT_*``). The
#: supervisor says each of them in one line, instead of "A supervised child exited"
#: (kittrial-5bb.168 follow-up: every other start failure is one line too).
WEB_PORT_TAKEN = 4
WEB_ADDRESS_UNAVAILABLE = 5
WEB_PORT_NOT_PERMITTED = 6
WEB_CERTIFICATE_UNUSABLE = 7


def child_exit_reason(web_status, settings, port):
    """What the supervisor says when a child has exited; each start failure is one sentence."""
    host = settings.get('http_host', '127.0.0.1')
    if web_status == WEB_PORT_TAKEN:
        return ('The web service did not start: port %d on %s is taken (something else holds it). '
                'Stop that, or run the service with another --port.' % (port, host))
    if web_status == WEB_ADDRESS_UNAVAILABLE:
        return ('The web service did not start: %s is not an address of this host, or its name does not resolve. '
                'Give an address of the host, or a name that resolves to one.' % host)
    if web_status == WEB_PORT_NOT_PERMITTED:
        return ('The web service did not start: port %d cannot be bound without privilege (a port below 1024 needs '
                'it). Use a port from 1024 to 65535.' % port)
    if web_status == WEB_CERTIFICATE_UNUSABLE:
        return ('The web service did not start: the certificate or key cannot be used. The line above it in '
                'http.log names the file and what is wrong.')
    return 'A supervised child exited; inspect service logs'


def run(root, logs, config, port, stop_seconds):
    if os.name != 'posix' or not sys.platform.startswith('linux'):
        raise ValueError('Foreground supervision requires Linux')
    if not 1024 <= port <= 65535 or not 1 <= stop_seconds <= 30:
        raise ValueError('Use an unprivileged HTTP port and a 1-30 second stop deadline')
    if not (root/'server.json').is_file() or not (root/'deployment.private.json').is_file():
        raise ValueError('Prepare this runtime before starting it')
    settings = service_config(config)
    # Before anything is started (kittrial-5bb.168 item 1): an unusable certificate or key is one
    # sentence naming the file, not a child's traceback and "A supervised child exited". The
    # warnings of item 4 are printed here too, at every start, and refuse nothing.
    checked = certificate_preflight(settings, root)
    if checked:
        # The pair that was CHECKED is the pair the child is given, with a relative path resolved
        # against the runtime root the child is spawned in (kittrial-5bb.168 review 1).
        settings = dict(settings, cert=checked[0], key=checked[1])
    for line in start_warnings(settings):
        print(line, file=sys.stderr, flush=True)
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
                        raise RuntimeError(child_exit_reason(web.poll(), settings, port))
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
