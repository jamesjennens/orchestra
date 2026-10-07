"""The client lines an office install gives out follow install/current (kittrial-5bb.182).

An office installation keeps each release under ``releases/<ID>`` and points ``current`` at
the one in use. ``Path(__file__).resolve()`` and ``sys.executable`` follow that link, so the
endpoint and the interpreter an install printed for its workers named the release that
printed them. After an upgrade the old folder stays on the host, so those clients kept
running the OLD kit against the new runtime while the service ran the new one, and the SSH
line ran a bare ``python3`` (platform-python 3.6 on RHEL 8, or absent).

These tests build that layout with real symlinks, so they run where symlinks exist.
"""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import client
import setup_assistant as sa

POSIX_LINKS = os.name == 'posix' and hasattr(os, 'symlink')
KEY_LINE = 'ssh-ed25519 ' + base64.b64encode(b'kittrial-5bb.182 lane key').decode() + ' lane'


def office_install(root):
    """Build ``root/releases/build-a`` with a kit, a bundled interpreter and ``current``.

    Returns (release_dir, kit_dir, interpreter). The interpreter is a real file whose name
    is what the release manifest records, so the bundled-interpreter lookup is exercised.
    """
    release = root / 'releases' / 'build-a'
    kit = release / 'kit'
    kit.mkdir(parents=True)
    for name in ('admin.py', 'endpoint.py', 'ssh_forced_command.py'):
        (kit / name).write_text('', encoding='utf-8')
    interpreter = release / 'python-runtime' / 'bin' / 'python3'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('', encoding='utf-8')
    (release / 'manifest.json').write_text(
        json.dumps({'schema_version': 1, 'python_executable': 'bin/python3'}), encoding='utf-8')
    (root / 'current').symlink_to('releases/build-a')
    return release, kit, interpreter


def worker_text(root):
    """The text add-project prints, with ``admin.__file__`` set inside the release."""
    release = root / 'releases' / 'build-a'
    with patch.object(admin, '__file__', str(release / 'kit' / 'admin.py')):
        return admin.worker_client_setup(root, 'alpha')


def no_current_install(root):
    """The live ordinary layout: ``root/kit -> root/releases/build-a``, no ``current``.

    ``admin.__file__`` is reached through the ``kit`` link, exactly as the live
    installations reach it. ``install_current_path`` leaves the release spelling alone, so
    the sentence add-project prints must say the paths name the release (item 1).
    """
    release = root / 'releases' / 'build-a'
    kit = release / 'kit'
    kit.mkdir(parents=True)
    for name in ('admin.py', 'endpoint.py', 'ssh_forced_command.py'):
        (kit / name).write_text('', encoding='utf-8')
    interpreter = release / 'python-runtime' / 'bin' / 'python3'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('', encoding='utf-8')
    (release / 'manifest.json').write_text(
        json.dumps({'schema_version': 1, 'python_executable': 'bin/python3'}), encoding='utf-8')
    (root / 'kit').symlink_to('releases/build-a')
    return release, kit, interpreter


def release_root_install(root):
    """``root/releases/build-a`` IS the kit and ``root/current`` points at it.

    A live-style install plus a ``current`` link somebody added: the path add-project
    resolves is the release directory itself, which ``resolved.parents`` alone never tested
    (item 3).
    """
    release = root / 'releases' / 'build-a'
    release.mkdir(parents=True)
    for name in ('admin.py', 'endpoint.py', 'ssh_forced_command.py'):
        (release / name).write_text('', encoding='utf-8')
    interpreter = release / 'python-runtime' / 'bin' / 'python3'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('', encoding='utf-8')
    (release / 'manifest.json').write_text(
        json.dumps({'schema_version': 1, 'python_executable': 'bin/python3'}), encoding='utf-8')
    (root / 'current').symlink_to('releases/build-a')
    return release, interpreter


def ssh_config(text):
    """The SSH client config JSON block add-project prints, parsed.

    The local-transport example also carries a ``python`` key, so a test that searches the
    whole text for ``"python":`` passes when the key is removed from THIS config alone
    (review mutant M6). Read the first JSON block instead: it is the SSH config.
    """
    start = text.index('{\n')
    end = text.index('\n}', start) + 2
    return json.loads(text[start:end])


def authorized_keys_payload(root, admin_file, interpreter, role='contributor'):
    """Run the real ``authorized-keys`` CLI and return its parsed JSON payload."""
    key_file = root / 'lane.pub'
    key_file.write_text(KEY_LINE + '\n', encoding='utf-8')
    out = io.StringIO()
    argv = ['admin.py', '--root', str(root / 'runtime'), 'authorized-keys',
            '--key-file', str(key_file), '--role', role]
    with patch.object(sys, 'argv', argv), patch('sys.stdout', out), \
            patch('sys.stderr', io.StringIO()), \
            patch.object(sys, 'executable', str(interpreter)), \
            patch.object(admin, '__file__', str(admin_file)):
        admin.main()
    return json.loads(out.getvalue())


@unittest.skipUnless(POSIX_LINKS, 'the installation layout needs real symlinks')
class CurrentPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'install'
        self.root.mkdir()
        self.release, self.kit, self.interpreter = office_install(self.root)
        self.current = self.root / 'current'

    def test_the_current_link_replaces_the_release_it_points_at(self):
        self.assertEqual(admin.install_current_path(self.release / 'kit' / 'endpoint.py'),
                         self.current / 'kit' / 'endpoint.py')
        self.assertEqual(admin.install_current_path(self.release / 'kit'),
                         self.current / 'kit')

    def test_a_path_outside_a_named_release_is_returned_unchanged(self):
        self.assertEqual(admin.install_current_path('/usr/bin/python3'), Path('/usr/bin/python3'))
        other = self.root / 'releases' / 'build-b' / 'kit' / 'endpoint.py'
        self.assertEqual(admin.install_current_path(other), other)

    def test_only_a_folder_named_releases_is_rewritten(self):
        # Mutant M2: any folder whose parent has a sibling `current` link pointing at it
        # counts, not only a folder named `releases`. The link the code consults is the
        # sibling of the release's PARENT (`install.parent/'current'`), so build a release
        # one level deeper: `outer/current -> inner/build-a`, where `inner` is the folder
        # that must NOT be treated as `releases`. Nothing under it may be rewritten.
        inner = self.root / 'outer' / 'inner'
        release = inner / 'build-a'
        (release / 'kit').mkdir(parents=True)
        path = release / 'kit' / 'endpoint.py'
        path.write_text('', encoding='utf-8')
        (inner.parent / 'current').symlink_to('inner/build-a')
        self.assertEqual(admin.install_current_path(path), path)
        self.assertIsNone(admin.install_current_link(path))

    def test_the_bundled_interpreter_is_found_through_current(self):
        with patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            self.assertEqual(admin.office_bundled_python(),
                             self.current / 'python-runtime' / 'bin' / 'python3')

    def test_worker_client_setup_prints_the_current_endpoint_and_interpreter(self):
        text = worker_text(self.root)
        endpoint = self.current / 'kit' / 'endpoint.py'
        interpreter = self.current / 'python-runtime' / 'bin' / 'python3'
        # The SSH config ITSELF must carry the interpreter key. The local-transport example
        # below also has a `python` key, so searching the whole text does not catch the key
        # being removed from this config (item 4, mutant M6): parse the first JSON block.
        config = ssh_config(text)
        self.assertEqual(config['host'], 'WORKER_SSH_HOST')
        self.assertEqual(config['endpoint'], str(endpoint))
        self.assertEqual(config['python'], str(interpreter))
        self.assertEqual(config['root'], str(self.root))
        # The release spelling the link resolves to must never be printed.
        self.assertNotIn('releases', text)
        self.assertNotIn(str(self.release), text)
        # The sentence says what was really printed: here, the install has a current link.
        self.assertIn('go through install/current, so an upgrade moves them', text)
        self.assertIn('the same install/current paths, so it also follows an upgrade', text)

    def test_worker_client_setup_prints_a_local_transport_example(self):
        text = worker_text(self.root)
        local = json.dumps({'transport': 'local',
                            'python': str(self.current / 'python-runtime' / 'bin' / 'python3'),
                            'endpoint': str(self.current / 'kit' / 'endpoint.py'),
                            'root': str(self.root)}, indent=2)
        self.assertIn(local, text)

    def test_worker_client_setup_says_a_host_project_is_not_on_the_web_yet(self):
        text = worker_text(self.root)
        self.assertIn('alpha', text)
        self.assertIn('superuser registers it', text)
        self.assertIn('POST /v1/projects', text)

    def test_authorized_keys_print_the_current_interpreter_and_endpoint(self):
        key_file = self.root / 'lane.pub'
        key_file.write_text(KEY_LINE + '\n', encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        argv = ['admin.py', '--root', str(self.root / 'runtime'), 'authorized-keys',
                '--key-file', str(key_file), '--role', 'contributor']
        with patch.object(sys, 'argv', argv), patch('sys.stdout', out), patch('sys.stderr', err), \
                patch.object(sys, 'executable', str(self.interpreter)), \
                patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            admin.main()
        payload = json.loads(out.getvalue())
        endpoint = str(self.current / 'kit' / 'endpoint.py')
        interpreter = str(self.current / 'python-runtime' / 'bin' / 'python3')
        self.assertEqual(payload['endpoint'], endpoint)
        self.assertEqual(payload['python'], interpreter)
        self.assertIn(endpoint, payload['contributor'])
        self.assertIn(interpreter, payload['contributor'])
        self.assertNotIn(str(self.release), payload['contributor'])

    def test_the_forced_command_and_the_client_config_name_the_same_endpoint(self):
        # The wrapper compares the caller's endpoint token exactly, so a config printed for
        # the worker and the forced command installed for the key must agree character for
        # character, and both must follow an upgrade.
        key_file = self.root / 'lane.pub'
        key_file.write_text(KEY_LINE + '\n', encoding='utf-8')
        out = io.StringIO()
        argv = ['admin.py', '--root', str(self.root / 'runtime'), 'authorized-keys',
                '--key-file', str(key_file), '--role', 'contributor']
        with patch.object(sys, 'argv', argv), patch('sys.stdout', out), \
                patch('sys.stderr', io.StringIO()), \
                patch.object(sys, 'executable', str(self.interpreter)), \
                patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            admin.main()
        self.assertIn(json.dumps(json.loads(out.getvalue())['endpoint']),
                      worker_text(self.root))


@unittest.skipUnless(POSIX_LINKS, 'the installation layout needs real symlinks')
class OrdinaryKitTests(unittest.TestCase):
    """The live layout ``kit -> releases/<ID>`` with no ``current`` link (item 1)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'install'
        self.root.mkdir()
        self.release, self.kit, self.interpreter = no_current_install(self.root)

    def text(self):
        # Reached through the `kit` link, exactly as the live installations reach it.
        with patch.object(admin, '__file__', str(self.root / 'kit' / 'admin.py')):
            return admin.worker_client_setup(self.root, 'alpha')

    def test_the_printed_paths_name_the_release(self):
        text = self.text()
        config = ssh_config(text)
        # An ordinary kit is not an office release, so there is no bundled-interpreter
        # manifest and `python` falls back to this interpreter (the reviewer's
        # sim-ordinary-kit.log shows /usr/bin/python3). What matters here is that the
        # endpoint names the release and no current spelling is printed.
        self.assertEqual(config['endpoint'], str(self.release / 'endpoint.py'))
        self.assertNotIn('/current/', config['python'])
        self.assertNotIn('/current/kit', text)
        self.assertNotIn('/current/python-runtime', text)

    def test_the_sentence_says_the_paths_name_the_release(self):
        text = self.text()
        self.assertNotIn('go through install/current', text)
        self.assertIn('no install/current link', text)
        self.assertIn('name the release that printed them', text)
        self.assertIn('again after an upgrade', text)

    def test_each_of_the_two_sentences_says_it_by_itself(self):
        """Review of kittrial-5bb.182 (mutants N10 and N13): 'again after an upgrade' stands in two sentences,
        so either could lose it, or say the opposite, and the test above still found the words."""
        text = self.text()
        no_link = text[text.index('This installation has no install/current link'):]
        no_link = ' '.join(no_link[:no_link.index(':\n')].split())
        self.assertIn('name the release that printed them and must be printed again after an upgrade', no_link)
        local = text[text.index('An agent that runs on the server itself'):]
        local = ' '.join(local[:local.index(':\n')].split())
        self.assertNotIn('follows an upgrade', local)
        self.assertNotIn('install/current', local)
        self.assertIn('name this release, so print them again after an upgrade', local)


@unittest.skipUnless(POSIX_LINKS, 'the installation layout needs real symlinks')
class ScheduledBackupLineTests(unittest.TestCase):
    """The printed schedule follows an upgrade like the client lines (item 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'install'
        self.root.mkdir()
        self.release, self.kit, self.interpreter = office_install(self.root)
        self.current = self.root / 'current'

    def test_the_backup_line_names_admin_py_and_the_interpreter_through_current(self):
        with patch.object(admin, '__file__', str(self.kit / 'admin.py')), \
                patch.object(sys, 'executable', str(self.interpreter)):
            line = admin.scheduled_backup_execstart(self.root)
        self.assertIn(str(self.current / 'python-runtime' / 'bin' / 'python3'), line)
        self.assertIn(str(self.current / 'kit' / 'admin.py'), line)
        self.assertNotIn(str(self.release), line)


@unittest.skipUnless(POSIX_LINKS, 'the installation layout needs real symlinks')
class ReleaseRootKitTests(unittest.TestCase):
    """The kit IS the release root with a sibling ``current`` link (item 3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'install'
        self.root.mkdir()
        self.release, self.interpreter = release_root_install(self.root)
        self.current = self.root / 'current'

    def test_add_project_and_authorized_keys_resolve_to_the_same_endpoint(self):
        # The forced-command wrapper compares the endpoint as one token, so the config
        # add-project prints and the line authorized-keys prints must agree exactly.
        payload = authorized_keys_payload(self.root, self.release / 'admin.py', self.interpreter)
        with patch.object(admin, '__file__', str(self.release / 'admin.py')):
            text = admin.worker_client_setup(self.root, 'alpha')
        self.assertEqual(payload['kit'], str(self.current))
        self.assertEqual(payload['endpoint'], str(self.current / 'endpoint.py'))
        self.assertIn(json.dumps(payload['endpoint']), text)
        self.assertNotIn(str(self.release / 'endpoint.py'), text)


class PlainCheckoutTests(unittest.TestCase):
    """Outside an office installation nothing changes: the resolved path is printed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kit = Path(self.tmp.name) / 'plainkit'
        self.kit.mkdir()
        (self.kit / 'endpoint.py').write_text('', encoding='utf-8')

    def test_no_manifest_means_no_bundled_interpreter(self):
        with patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            self.assertIsNone(admin.office_bundled_python())

    def test_worker_client_setup_keeps_the_resolved_endpoint(self):
        with patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            text = admin.worker_client_setup(Path('/srv/rt'), 'alpha')
        resolved = Path(os.path.realpath(str(self.kit / 'endpoint.py')))
        self.assertIn('"endpoint": ' + json.dumps(str(resolved)), text)
        self.assertNotIn('/current/kit', text.replace('install/current', ''))

    def test_a_manifest_that_cannot_be_read_falls_back_instead_of_crashing(self):
        release = self.kit.parent
        (release / 'python-runtime' / 'bin').mkdir(parents=True)
        (release / 'python-runtime' / 'bin' / 'python3').write_text('', encoding='utf-8')
        (release / 'manifest.json').write_text('[' * 3000 + ']' * 3000, encoding='utf-8')
        with patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            self.assertIsNone(admin.office_bundled_python())

    def test_a_manifest_naming_an_interpreter_outside_python_runtime_is_refused(self):
        release = self.kit.parent
        for relative in ('../../etc/passwd', '/usr/bin/python3', '', 'bin/missing'):
            with self.subTest(relative=relative):
                (release / 'manifest.json').write_text(
                    json.dumps({'schema_version': 1, 'python_executable': relative}), encoding='utf-8')
                with patch.object(admin, '__file__', str(self.kit / 'admin.py')):
                    self.assertIsNone(admin.office_bundled_python())


class SshInterpreterTests(unittest.TestCase):
    """The SSH remote command runs the interpreter the config names, on the SERVER."""

    BASE = {'host': 'sample', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state'}

    def test_the_configured_interpreter_is_used(self):
        argv, label = client._ssh_argv(dict(self.BASE, python='/srv/current/python-runtime/bin/python3'))
        self.assertEqual(label, 'SSH')
        self.assertEqual(argv[-1],
                         '/srv/current/python-runtime/bin/python3 /srv/kit/endpoint.py --root /srv/state')

    def test_no_interpreter_key_keeps_python3_exactly(self):
        argv, _ = client._ssh_argv(dict(self.BASE))
        self.assertEqual(argv[-1], 'python3 /srv/kit/endpoint.py --root /srv/state')

    def test_an_unsafe_interpreter_is_refused_before_any_command(self):
        for value in ('python3 -X utf8', '-E', '', 'a b'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    client._ssh_argv(dict(self.BASE, python=value))

    def test_forced_command_mode_still_sends_only_the_endpoint(self):
        argv, _ = client._ssh_argv(dict(self.BASE, forced_command=True,
                                        python='/srv/current/python-runtime/bin/python3'))
        self.assertEqual(argv[-1], '/srv/kit/endpoint.py')


class SetupAssistantSshInterpreterTests(unittest.TestCase):
    """The tool that writes configs can record the interpreter the ssh transport now uses."""

    def request(self, **overrides):
        values = dict(project='example', config_path='client.local.json', transport='ssh',
                      host='example-host', endpoint='/srv/orchestra/endpoint.py',
                      root='/srv/orchestra/runtime')
        values.update(overrides)
        return sa.SetupRequest(**values)

    def test_an_ssh_config_records_the_interpreter_when_one_is_given(self):
        config = sa.build_config(self.request(python='/srv/current/python-runtime/bin/python3'))
        self.assertEqual(config['python'], '/srv/current/python-runtime/bin/python3')
        self.assertEqual(config['transport'], 'ssh')

    def test_an_ssh_config_without_one_keeps_the_client_default(self):
        self.assertNotIn('python', sa.build_config(self.request()))


if __name__ == '__main__':
    unittest.main()
