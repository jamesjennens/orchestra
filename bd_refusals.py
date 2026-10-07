"""When bd itself says no (kittrial-5bb.185).

bd refuses a command in several ways, and the endpoint handed its exit code on as it
came. Everything that was not 0 was then kept as an operation whose outcome is unknown,
and the web service told the caller "the operation may have committed; reconcile with
the same idempotency key" for a title bd found empty, a title of 600 characters, a task
that does not exist: for ever, because the same request gets the same answer.

``refusal`` recognises a refusal by its FORM and its SENTENCE together. The form alone
is not enough: a failure after a write could be printed in the same form, and calling
that "nothing happened" would be the worse mistake. So an answer counts as a refusal
only when it is one of bd's own sentences below, each of which bd says before it writes
(measured on bd 1.2.2 with the tracker compared before and after; the test
``tests/test_bd_refusals.py`` runs every row against a real bd when there is one).
Anything else stays what it was: an outcome that is not known.

Pure, without imports of the kit.
"""
import json
import re

NOT_FOUND, INVALID = 'not-found', 'invalid'
#: bd's own claim (`update ID --claim`) refused: the row is somebody else's, or it is not open
#: (kittrial-5bb.187). bd checks and writes a claim in one step, so of several claims of one
#: free row exactly one is carried out and the others are told who has it.
CLAIMED, NOT_CLAIMABLE = 'claimed', 'not-claimable'
HOLDER = re.compile(r'^issue already claimed by (?P<holder>[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95})$')
STATUS = re.compile(r'^issue not claimable: status (?P<status>[a-z_]{1,40})$')
#: bd's own sentences for a refusal it makes before writing, as bd 1.2.2 prints them.
SENTENCES = (
    (NOT_FOUND, re.compile(r'\bno issues? found matching\b')),
    (CLAIMED, HOLDER),
    (NOT_CLAIMABLE, STATUS),
    (INVALID, re.compile(r'^validation failed for issue\b')),
    (INVALID, re.compile(r'\bcannot be empty$')),
    (INVALID, re.compile(r'^invalid (?:priority|status) ')),
    (INVALID, re.compile(r'^title "[^\n]*" looks like a flag\b')),
)
#: How much of bd's sentence is handed on. It can carry the caller's own text (a title).
SHOWN = 240
ERROR_LINE = re.compile(r'Error(?: (?:resolving|fetching|claiming) [^:\n]{1,200})?: (.+)\Z')


def said(returncode, stdout, stderr):
    """bd's one sentence of refusal in the forms it uses, or None.

    * exit 1, and standard output is one JSON object ``{"error": TEXT, "schema_version": N}``
      and nothing else (``--json`` commands that got as far as their own checks);
    * exit 1, nothing on standard output, and exactly one line of standard error that
      begins ``Error: `` or ``Error resolving ID: `` (argument and validation errors).
      bd's warning about ``beads.role`` and its hints are other lines and are ignored.
    """
    if returncode != 1 or not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    text = stdout.strip()
    if text:
        try:
            answer = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(answer, dict) or set(answer) != {'error', 'schema_version'} \
                or not isinstance(answer['error'], str):
            return None
        return answer['error'].strip() or None
    errors = [line for line in stderr.splitlines() if line.startswith('Error')]
    if len(errors) != 1:
        return None
    found = ERROR_LINE.match(errors[0].rstrip())
    return found.group(1).strip() if found else None


def refusal(returncode, stdout, stderr):
    """``(kind, sentence)`` when bd refused before writing, or None when that is not known.

    ``kind`` is ``NOT_FOUND`` (bd could not find the row that was named) or ``INVALID``.
    """
    sentence = said(returncode, stdout, stderr)
    if sentence is None or '\n' in sentence:
        return None
    for kind, pattern in SENTENCES:
        if pattern.search(sentence):
            return kind, sentence[:SHOWN]
    return None


def holder(detail):
    """Who has the row, from the last line of a ``CLAIMED`` refusal as the endpoint hands it on, or None."""
    found = HOLDER.match(str(detail or '').rpartition('bd refused: ')[2].strip())
    return found.group('holder') if found else None


def status(detail):
    """The row's status, from the last line of a ``NOT_CLAIMABLE`` refusal, or None."""
    found = STATUS.match(str(detail or '').rpartition('bd refused: ')[2].strip())
    return found.group('status') if found else None


def envelope(kind, sentence):
    """The endpoint's answer for a refusal of bd's: a refusal (2), bd's sentence, and which kind."""
    return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: bd refused: %s\n' % sentence, 'refused': kind}
