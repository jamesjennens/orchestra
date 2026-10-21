"""The server address the kit hands people in commands, checked once (kittrial-5bb.360).

The service's ``public_url`` (an operator's setting: office.json, or ``http_service.py
--public-url``) is copied into the set-up commands a person pastes into a shell: the web
wizard's tests and self-check, the credential file rewrite, AGENT.md's curl lines and the
server's own setup snippet. Before this, only its scheme and host were checked, so
``https://host/$(touch X)`` passed and ran in the person's shell. Only an operator can set
it, so it is not a worker's attack, but a mistyped or hostile setting must never become a
command.

The rule accepts every real address form: a DNS name, an IPv4 address or a bracketed IPv6
address, an optional port 1..65535, and an optional plain path of letters, digits and
``. _ ~ - /`` with ``%XX`` escapes. It refuses userinfo, a query or a fragment, any other
character (quotes, ``$``, backtick, ``;``, ``|``, ``&``, ``(``, ``)``, ``<``, ``>``,
whitespace and the rest), and a ``%XX`` escape of a character it refuses. The refusal is ONE
sentence naming the setting, where it came from, and the first character it refuses.

The commands quote the address as well (defence in depth): ``sh_quote`` and ``ps_quote``.
"""
import ipaddress
import re

#: What a host may be: a DNS name (labels of letters, digits, hyphens and underscores, as
#: internal names sometimes carry), an IPv4 address, or an IPv6 address in brackets.
_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*\.?')
_ADDRESS = re.compile(
    r'(?P<scheme>[Hh][Tt][Tt][Pp][Ss]?)://'
    r'(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)'
    r'(?::(?P<port>[0-9]{1,5}))?'
    r'(?P<path>/[A-Za-z0-9._~/%-]*)?')
#: The characters an address may hold at all; anything else is named in the refusal.
_ALLOWED = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_~/:[]%')
#: What a %XX escape may stand for in a path: printable, and not a character a shell or a
#: command line gives a meaning to.
_ESCAPE_REFUSED = set('\'"$`;|&()<>\\!*?{}[]#%') | {chr(c) for c in range(0x21)} | {'\x7f'}


def _show(value):
    text = value if isinstance(value, str) and value else repr(value)
    return text if len(text) <= 200 else text[:200] + '...'


def public_url_problem(value, source, setting='public_url'):
    """``None`` when ``value`` is a server address the kit may put in a command, else one sentence.

    ``source`` says where the value came from (``office.json``, ``--public-url``), so the
    operator knows what to correct; ``setting`` names it."""
    where = '%s from %s' % (setting, source)
    if not isinstance(value, str) or not value:
        return '%s must be an http or https address, got %s.' % (where, _show(value))
    for character in value:
        if character not in _ALLOWED:
            return ('%s has the character %r, which a server address may not hold: give an http or https address '
                    'with a host, an optional port and a path of letters, digits, . _ ~ - / and %%XX escapes '
                    '(got %s).' % (where, character, _show(value)))
    match = _ADDRESS.fullmatch(value)
    if not match:
        return ('%s must be an http or https address with a host, an optional port and an optional path '
                '(got %s).' % (where, _show(value)))
    host = match.group('host')
    if host.startswith('['):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return '%s has a bracketed host that is not an IPv6 address (got %s).' % (where, _show(value))
    elif not _NAME.fullmatch(host):
        return '%s has a host that is not a name or an address (got %s).' % (where, _show(value))
    port = match.group('port')
    if port is not None and not 1 <= int(port) <= 65535:
        return '%s has a port outside 1..65535 (got %s).' % (where, _show(value))
    path = match.group('path') or ''
    for escape in re.finditer(r'%(..)?', path):
        code = escape.group(1)
        if code is None or not re.fullmatch(r'[0-9A-Fa-f]{2}', code):
            return '%s has a %% that is not a %%XX escape (got %s).' % (where, _show(value))
        if chr(int(code, 16)) in _ESCAPE_REFUSED:
            return ('%s has the escape %%%s, which stands for a character a server address may not hold '
                    '(got %s).' % (where, code.upper(), _show(value)))
    return None


def sh_quote(text):
    """``text`` as one POSIX shell word: single quotes, nothing expanded (``'`` as ``'\\''``)."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def ps_quote(text):
    """``text`` as one PowerShell literal: single quotes, nothing expanded (``'`` doubled)."""
    return "'" + str(text).replace("'", "''") + "'"
