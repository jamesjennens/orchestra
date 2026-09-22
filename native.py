"""Native (bd) process results: keep success JSON separate from warnings.

The endpoint protocol carries stdout and stderr separately. A native command can
still emit warnings on stderr with exit status 0; they must reach the caller's
stderr instead of being dropped or concatenated onto the success JSON. Nonzero
exits become a labelled, length-capped ValueError so a raw private payload is
never dumped wholesale into errors or logs.
"""
import subprocess

DIAGNOSTIC_LIMIT = 500
TIMEOUT = 120


def argv(root, path, actor, command, scoped=True):
    """Build the fixed native argv. Only trusted kit values enter it."""
    prefix = [str(root / 'bin' / 'bd'), '--directory', str(path), '--sandbox']
    if scoped:
        prefix += ['--actor', actor]
    return [*prefix, *command]


def run(command, env, timeout=TIMEOUT):
    return subprocess.run(command, env=env, capture_output=True, text=True,
                          encoding='utf-8', timeout=timeout)


def failure(completed):
    detail = (completed.stderr or completed.stdout or '').strip()
    if len(detail) > DIAGNOSTIC_LIMIT:
        detail = detail[:DIAGNOSTIC_LIMIT] + '... [truncated]'
    return 'Native command failed (%d)%s' % (completed.returncode, ': ' + detail if detail else '')


def split(completed):
    """Return (stdout, warnings); raise a bounded, labelled error on nonzero exit."""
    if completed.returncode:
        raise ValueError(failure(completed))
    return completed.stdout, completed.stderr or ''
