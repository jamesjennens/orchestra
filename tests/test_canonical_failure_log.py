"""A 124 keeps its caller sentence and puts a bounded, redacted cause in the log (kittrial-5bb.86).

Return code 124 means "the identity is reserved and the outcome is unknown": a real timeout OR
any other exception after a possible write. The caller must keep the classified 503 sentence;
the endpoint's own line is the only place the cause can be read, and it is operator-only.

These cases need no service, no endpoint and no network: ``_checked`` is a classmethod over one
reply envelope, so they also run on a machine with no ``bd``.
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import http_service


class FailureLogCase(unittest.TestCase):
    checked = staticmethod(lambda reply, **more: http_service.EndpointBackend._checked(reply, 'bd', **more))

    def logged(self, reply, **more):
        """The HttpError and the service log of one reply."""
        log = io.StringIO()
        with contextlib.redirect_stderr(log), self.assertRaises(http_service.HttpError) as caught:
            self.checked(reply, **more)
        return caught.exception, log.getvalue()

    def test_the_cause_is_logged_and_the_caller_keeps_its_sentence(self):
        error, log = self.logged({
            'returncode': 124, 'stdout': '',
            'stderr': 'Effect raised after the reservation (ValueError: boom); outcome unknown. '
                      'Reconcile canonical state before retrying.\n'})
        self.assertEqual('Canonical command failed after it was reserved; outcome may be unknown',
                         error.message)
        self.assertNotIn('boom', error.message)
        self.assertIn('ValueError: boom', log)
        self.assertTrue(log.startswith('canonical: '), log)

    def test_a_real_timeout_still_says_timed_out(self):
        error, log = self.logged({
            'returncode': 124, 'stdout': '',
            'stderr': 'Command timed out; mutation outcome may be uncertain. Inspect state before retrying.\n'})
        self.assertEqual('Canonical command timed out; outcome may be unknown', error.message)
        self.assertIn('Command timed out', log)

    def test_a_reading_124_is_logged_too(self):
        error, log = self.logged({'returncode': 124, 'stdout': '',
                                  'stderr': 'Effect raised after the reservation (OSError: disk full)\n'},
                                 reading=True)
        self.assertEqual('unavailable', error.code)
        self.assertIn('OSError: disk full', log)

    def test_the_log_line_is_bounded(self):
        error, log = self.logged({'returncode': 124, 'stdout': '',
                                  'stderr': 'Effect raised (ValueError: ' + 'x' * 5000 + ')\n'})
        self.assertEqual('Canonical command failed after it was reserved; outcome may be unknown',
                         error.message)
        self.assertLess(len(log.strip()), 400, len(log))
        self.assertNotIn('x' * 200, log)

    def test_paths_and_credentials_are_masked(self):
        error, log = self.logged({
            'returncode': 124, 'stdout': '',
            'stderr': 'Effect raised (ValueError: cannot read /srv/other-project/private '
                      'token=abcdef123456); outcome unknown.\n'})
        self.assertNotIn('/srv/other-project/private', log)
        self.assertNotIn('abcdef123456', log)
        self.assertIn('<path>', log)
        self.assertIn('<redacted>', log)

    def test_each_credential_shape_is_masked(self):
        for said in ('token=supersecretvalue', 'secret: supersecretvalue',
                     'password=supersecretvalue', 'api_key=supersecretvalue',
                     'authorization: supersecretvalue', 'Bearer supersecretvalue'):
            with self.subTest(said=said.split('=')[0].split(':')[0]):
                _, log = self.logged({'returncode': 124, 'stdout': '',
                                      'stderr': 'Effect raised (ValueError: %s)\n' % said})
                self.assertNotIn('supersecretvalue', log)
                self.assertIn('<redacted>', log)

    def test_a_control_character_is_shown_not_emitted(self):
        _, log = self.logged({'returncode': 124, 'stdout': '',
                              'stderr': 'Effect raised (ValueError: \x1b[31mred\x1b[0m)\n'})
        self.assertNotIn('\x1b', log)
        self.assertIn('\\x1b', log)

    def test_an_empty_stderr_logs_nothing(self):
        _, log = self.logged({'returncode': 124, 'stdout': '', 'stderr': '\n'})
        self.assertEqual('', log)

    def test_a_non_124_reply_is_not_logged_as_a_124(self):
        for code in (1, 2, 75, 126):
            with self.subTest(code=code):
                with contextlib.redirect_stderr(io.StringIO()) as log, \
                        self.assertRaises(http_service.HttpError):
                    self.checked({'returncode': code, 'stdout': '', 'stderr': 'ValueError: refused\n'})
                self.assertNotIn('canonical:', log.getvalue())


if __name__ == '__main__':
    unittest.main()
