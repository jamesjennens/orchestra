"""Read-only, fixed-catalog onboarding from the installed kit and project."""
import re
from pathlib import Path
import regular_file
from version import line, report

DOCUMENTS = {
    'start': 'docs/WORKER_START.md',
    'workflow': 'docs/WORKFLOW.md',
    'worker-guide': 'docs/WORKER_GUIDE.md',
    'host-notes': 'docs/HOST_NOTES.md',
    'sessions': 'docs/SESSIONS.md',
    'reviews': 'docs/REVIEWS.md',
    'contribution-template': 'templates/CONTRIBUTION.json',
    'review-request-template': 'templates/REVIEW_REQUEST.json',
    'review-response-template': 'templates/REVIEW_RESPONSE.json',
    'handoff-template': 'templates/HANDOFF.json',
    'briefings': 'docs/BRIEFINGS.md',
    'cli-contract': 'docs/CLI_CONTRACT.md',
    'operations': 'docs/OPERATIONAL_WORKFLOW.md',
    # The served template is the page that states the limits and the lane_state shape;
    # the bare payload it embeds stays in templates/CHECKPOINT.json (kittrial-5bb.341).
    'checkpoint-template': 'templates/CHECKPOINT.md',
    'task-template': 'templates/TASK.md',
    'report-template': 'templates/REPORT.md',
    'decision-template': 'templates/DECISION.md',
    # How a worker finds its work and how a coordinator reaches it, and the prompts a worker
    # is started and woken with (kittrial-5bb.226, kittrial-5bb.219). The web service shows
    # these same files to every member (WEB_DOCUMENTS below), so each text has one source.
    'finding-work': 'docs/FINDING_WORK.md',
    'worker-prompt': 'templates/WORKER_PROMPT.md',
    'poll-prompt': 'templates/WORKER_POLL_PROMPT.md',
    'poll-prompt-agent': 'templates/AGENT_POLL_PROMPT.md',
}
#: The catalogued documents the WEB service hands to any signed-in member or agent
#: (``GET /v1/docs/NAME``): what a member reads to learn how work is found, and the
#: prompts. Nothing of a project and nothing private is in them; the rest of the
#: catalogue stays with the client's ``docs``.
WEB_DOCUMENTS = ('finding-work', 'worker-prompt', 'poll-prompt', 'poll-prompt-agent')
#: A recurring prompt stays short: it carries no state and no rules (kittrial-5bb.219).
#: The bound is on the prompt itself, the fenced block of its file, in characters.
POLL_PROMPT_LIMIT = 2000
PROJECT_LIMIT = 8000
#: The start document is put in front of every worker by ``onboard``: it is kept short.
START_LIMIT = 8000
#: The bound for a document the KIT ships (every other name in ``DOCUMENTS``), in bytes.
#:
#: It is not a limit on how much the kit may say. Those documents are the kit's own files:
#: nobody at an installation writes them, and nobody there can "shorten" one. The bound
#: protects against a different thing: the file at that path being replaced by something
#: that is not the document (a log, a dump, a device), which the endpoint would otherwise
#: read and send without limit. One megabyte is eight times the longest document today
#: (CLI_CONTRACT.md, 118 KB). The 2 MB figure in client.py MAX_WIRE is a limit on requests only;
#: there is no answer cap of that size. tests/test_document_catalog.py fails when a catalogued
#: document is missing, empty or over its bound, so a document cannot outgrow it unnoticed.
#: The limit this replaces, 64,000 bytes, was one number for the kit's documents and for
#: text people write; REVIEWS.md and CLI_CONTRACT.md outgrew it and were refused on every
#: installation (kittrial-5bb.151).
KIT_DOCUMENT_LIMIT = 1_000_000
ENDPOINT_REFERENCE = re.compile(r'(?:[A-Za-z]:)?[\\/][A-Za-z0-9_.~\\/-]*\.py')
ENDPOINT_REFUSAL = 'serves only'

def write_project(path, text):
    import os
    import tempfile
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > PROJECT_LIMIT:
        raise ValueError('Project onboarding must be nonempty UTF-8 text up to 8000 bytes')
    path = Path(path)
    if path.is_symlink():raise ValueError('Project onboarding must not be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.onboarding-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

#: The first line of onboarding text that a project owner or a coordinator set from
#: the web interface (kittrial-5bb.118 part 2; the wording widened in kittrial-5bb.349
#: because a coordinator or a granted personal agent may write it too). The kit writes
#: it; the writer's text follows. It is part of the stored document on purpose: it
#: travels with a backup and a restore, and a kit that predates it shows it too, so
#: wherever the text is read it says who wrote it and that it is information, not an
#: instruction from the server's operator.
WEB_HEADER = ('[Written by the project\'s owner or a coordinator in the web interface. It is information about '
              'the project, not an instruction from the operator of this server.]')
#: The first line older kits wrote. Documents that carry it are still web-written:
#: ``split_web`` strips either line so an existing project keeps showing and saving
#: the writer's own text after the wording change (kittrial-5bb.349).
LEGACY_WEB_HEADERS = (
    '[Written by an owner of this project in the web interface. It is information about the project, '
    'not an instruction from the operator of this server.]',
)


#: Every line an owner wrote is stored and shown behind this mark, under the kit's line
#: (review 01a109cc): a heading or a bracketed line in the owner's text is then visibly
#: the owner's, and cannot pass for the end of the block or for a line of the kit. It is
#: in the stored document, not added on delivery, so every reader shows it: `onboard`,
#: `docs project`, a restored backup and a kit older than this one.
OWNER_PREFIX = '| '
OWNER_EMPTY = '|'
#: Where the operator's own text is kept when an owner's first text replaces it.
OPERATOR_COPY = 'ONBOARDING.operator-copy.md'


def web_document(text):
    """The stored document for owner-written onboarding text, or raise ValueError.

    The same size limit as ``set-onboarding`` (the kit's line and the line marks count
    toward it), and the plain-text rule of the guidance channel: no control, bidi,
    zero-width, invisible or format character, so what an owner types is what every
    reader sees. Lines end with a line feed; a carriage return that is not part of a
    CRLF pair is refused, and so is text that is not valid Unicode (a lone surrogate).
    """
    from guidance import validate_text
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Project onboarding must be nonempty text')
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Project onboarding must be valid Unicode text (it holds half of a surrogate pair)') from None
    text = text.replace('\r\n', '\n')
    if '\r' in text:
        raise ValueError('Project onboarding must be plain lines (a carriage return without a line feed is not one)')
    try:
        validate_text(text)
    except ValueError as error:
        raise ValueError(str(error).replace('Guidance', 'Project onboarding')) from None
    lines = text.strip('\n').split('\n')
    marked = '\n'.join(OWNER_PREFIX + line if line.strip() else OWNER_EMPTY for line in lines)
    document = WEB_HEADER + '\n\n' + marked + '\n'
    if len(document.encode('utf-8')) > PROJECT_LIMIT:
        used = len((WEB_HEADER + '\n\n\n').encode('utf-8')) + len(OWNER_PREFIX) * len(lines)
        raise ValueError('Project onboarding must be at most %d bytes (%d of them are used by the line that says who '
                         'wrote it and by the mark before each of your %d lines)' % (PROJECT_LIMIT, used, len(lines)))
    return document


def split_web(document):
    """``(written_by_owner, text)``: the writer's own text when the document carries the kit's line.

    The line marks are taken off again, so the web editor shows what the writer typed.
    Both the current and the pre-kittrial-5bb.349 first line are recognised, so an
    existing project keeps editing its own text. A line without its mark (the file was
    edited on the host) is kept as it is.
    """
    if isinstance(document, str):
        for header in (WEB_HEADER,) + LEGACY_WEB_HEADERS:
            if document.startswith(header + '\n'):
                body = document[len(header):].strip('\n').split('\n')
                plain = ['' if line == OWNER_EMPTY else line[len(OWNER_PREFIX):] if line.startswith(OWNER_PREFIX) else line
                         for line in body]
                return True, '\n'.join(plain) + '\n'
    return False, document


def web_action(project_path, project, request, authority_config):
    """The ``set-onboarding`` endpoint action: an owner sets or clears the text. The envelope.

    Only for the authenticated web service, for a current account or personal agent
    that holds the coordination capability on THIS project; re-checked against
    the live authority store under its lock by ``run_guarded``. The caller holds the
    project's coordination lock. ``args`` is ``['set']`` with the text attached as
    ``text``, or ``['clear']``. Clearing removes only text an owner set from the web: the
    operator's own document is never removed from here.
    """
    import json
    from http_authority import journal_path, run_guarded
    from http_authority import AuthorityDenied, CAP_COORDINATE
    from web_guidance import require_authority, _FileEffect

    def authorize():
        return require_authority(request, authority_config, CAP_COORDINATE, project)
    args = request.get('args')
    if args not in (['set'], ['clear']):
        raise ValueError('Use set-onboarding set (with the text attached) or set-onboarding clear')
    attachments = request.get('attachments') or {}
    if not isinstance(attachments, dict) or set(attachments) != ({'text'} if args == ['set'] else set()):
        raise ValueError('Only onboarding set carries a text attachment')
    attachment = attachments.get('text')
    if args == ['set'] and (not isinstance(attachment, dict) or set(attachment) != {'flag', 'text'} or attachment.get('flag') != '--file'):
        raise ValueError('Onboarding set needs one literal text attachment')
    text = attachment.get('text') if isinstance(attachment, dict) else None
    tracker = _FileEffect()
    target = Path(project_path) / 'ONBOARDING.md'

    def effect():
        try:
            if args == ['set']:
                document = web_document(text)
                if target.is_symlink():
                    raise ValueError('Project onboarding must not be a symlink')
                kept = None
                if target.is_file():
                    previous = target.read_text(encoding='utf-8-sig')
                    if previous.strip() and not split_web(previous)[0]:
                        # The operator's own text is about to be replaced: it is kept beside the
                        # document, so an owner's edit never destroys what an operator wrote.
                        copy = Path(project_path) / OPERATOR_COPY
                        if copy.is_symlink():
                            raise ValueError('The copy of the operator\'s onboarding text must not be a symlink')
                        tracker.before_write()
                        write_project(copy, previous)
                        kept = OPERATOR_COPY
                tracker.before_write()
                write_project(target, document)
                result = {'state': 'set', 'source': 'web', 'bytes': len(document.encode('utf-8')),
                          'operator_text_kept_as': kept}
            else:
                if target.is_symlink():
                    raise ValueError('Project onboarding must not be a symlink')
                if target.is_file():
                    if not split_web(target.read_text(encoding='utf-8-sig'))[0]:
                        raise ValueError('The onboarding text here was set by an operator on the server; only an '
                                         'operator changes or removes it')
                    tracker.before_write()
                    target.unlink()
                result = {'state': 'not-set', 'source': None, 'bytes': 0}
        except ValueError as refusal:
            if tracker.attempted_write:
                raise  # The retained operator copy or document may already have changed.
            return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % refusal}
        return {'returncode': 0, 'stdout': json.dumps(result) + '\n', 'stderr': ''}
    try:
        return run_guarded(request, journal_path(project_path), effect, authority_config=authority_config,
                           require_authority=True, identity_check=authorize, runner=tracker)
    except AuthorityDenied as error:
        return dict(returncode=126, stdout='', stderr=error.message + '\n', authority_status=error.status)


def _document_unreadable(name, error):
    """The sentence for a document this process cannot read, name or look up.

    Never the absolute path of a host file (review 01a12be9), and it NAMES the document
    (kittrial-5bb.262 C5): "cannot be read on this installation (...)" with no subject left the
    reader guessing which document was refused.
    """
    reason = getattr(error, 'strerror', None) or type(error).__name__
    return ValueError('The document "%s" cannot be read on this installation (%s); ask the operator'
                      % (name, reason.lower()))


def read_document(base, relative, limit, kit_document=None):
    """The whole text of one document under ``base``, or a refusal; never a part of it.

    ``limit`` is in bytes and has no default: the caller says which bound applies
    (``PROJECT_LIMIT`` for text an owner or operator wrote, ``START_LIMIT``,
    ``KIT_DOCUMENT_LIMIT``). ``kit_document`` is the catalogue name of a document the kit
    ships: a refusal for one says what is wrong with the installation, not that somebody
    should shorten it. A document this process cannot even look up - a directory on the way
    in with no search permission raises from ``is_symlink``/``is_file`` before the read - is
    refused with the same path-free sentence as a failed read, naming the document
    (kittrial-5bb.262 C2, C5).
    """
    base = Path(base).resolve()
    path = base / relative
    try:
        if any(p.is_symlink() for p in [path, *path.parents] if p != base and base in p.parents):
            raise ValueError('Onboarding document must not use symlinks')
        if not path.resolve().is_relative_to(base):
            raise ValueError('Document outside configured root')
        if not path.is_file():
            raise ValueError('Onboarding document missing; ask the operator to configure/update this installation')
    except OSError as error:
        raise _document_unreadable(relative, error) from None
    try:
        with regular_file.open_regular(path) as f:
            data = f.read(limit + 1)
    except (OSError, ValueError) as error:
        # Never the absolute path of a host file (review 01a12be9).
        raise _document_unreadable(relative, error) from None
    if len(data) > limit and kit_document is not None:
        # The bound is NAMED for what it is (kittrial-5bb.262 C5). Two bounds apply to the start
        # document: ``onboard`` and ``docs --status`` read it with the tighter START_LIMIT,
        # ``docs start`` with the kit-document bound. The old sentence - "The start document is
        # larger than the start document may be" - was circular and never said which one refused
        # it, so ``docs start`` over the kit bound read as the start bound and the other way round.
        if kit_document != 'start':
            raise ValueError('The kit document "%s" is larger than a kit document can be (over %d bytes), so the file '
                             'at that place in this installation is not the one the kit ships. Nothing was returned; '
                             'ask the operator to reinstall the kit. The same text is in the repository (%s)'
                             % (kit_document, limit, relative))
        bound = ('the kit-document bound' if limit == KIT_DOCUMENT_LIMIT
                 else 'the start bound, which is tighter than the kit-document bound')
        raise ValueError('The kit document "%s" is larger than %s (over %d bytes), so the file at that place in this '
                         'installation is not the one the kit ships. Nothing was returned; ask the operator to '
                         'reinstall the kit. The same text is in the repository (%s)'
                         % (kit_document, bound, limit, relative))
    if len(data) > limit:
        raise ValueError('Onboarding document exceeds size limit; operator must shorten it (no partial instructions returned)')
    result = data.decode('utf-8-sig')
    if not result.strip():raise ValueError('Onboarding document is empty')
    return result

def read_kit_document(kit, name):
    """A catalogued document the kit ships, whole (``docs NAME``)."""
    return read_document(kit, DOCUMENTS[name], KIT_DOCUMENT_LIMIT, kit_document=name)


def catalog_status(kit, project_path=None):
    """Every catalogued name as served (with its size) or refused (with its sentence).

    One read for release live verification (kittrial-5bb.153 item 4): the generated
    live-verify script can ask for every name at once and see which are served and
    which are refused, instead of only ``onboard``. ``project`` is included when
    ``project_path`` is given. Never raises for a name: a refusal is a row.
    """
    names = (['project'] if project_path is not None else []) + list(DOCUMENTS)
    rows = []
    for name in names:
        try:
            # The text is not kept: this is a status read, and the unused binding was removed
            # (kittrial-5bb.262 C6).
            if name == 'project':
                read_document(project_path, 'ONBOARDING.md', PROJECT_LIMIT)
                size = (Path(project_path) / 'ONBOARDING.md').stat().st_size
            elif name == 'start':
                # onboard reads start with the tighter START_LIMIT: a live check that only
                # asked --status would pass where no worker can start (review 01a12be9).
                read_document(kit, DOCUMENTS['start'], START_LIMIT, kit_document='start')
                size = (Path(kit) / DOCUMENTS['start']).stat().st_size
            else:
                read_kit_document(kit, name)
                size = (Path(kit) / DOCUMENTS[name]).stat().st_size
            rows.append({'name': name, 'served': True, 'bytes': size})
        except ValueError as error:
            rows.append({'name': name, 'served': False, 'refused': str(error)})
        except OSError as error:
            # Never the absolute path of a host file (review 01a12be9): the short reason only,
            # and the document is named (kittrial-5bb.262 C5).
            rows.append({'name': name, 'served': False, 'refused': str(_document_unreadable(name, error))})
    return rows


def prompt_of(text):
    """The prompt a document carries, or None: everything between its first line that is
    exactly ```text and its LAST line that is exactly ```.

    A prompt document says above the block what to replace; the block is what is pasted.
    The last fence, not the next one: the worker prompt shows commands in a fenced block
    of its own inside the prompt.
    """
    lines = (text if isinstance(text, str) else '').splitlines()
    fences = [index for index, line in enumerate(lines) if line.rstrip() == '```']
    starts = [index for index, line in enumerate(lines) if line.rstrip() == '```text']
    if not starts or not fences or fences[-1] <= starts[0]:
        return None
    return '\n'.join(lines[starts[0] + 1:fences[-1]]).rstrip('\n') + '\n'


PLACEHOLDER = re.compile(r'\bREPLACE_[A-Z][A-Z_]*[A-Z]\b')


def placeholders(text):
    """The named values a prompt leaves to be replaced, in the order they first appear."""
    seen = []
    for name in PLACEHOLDER.findall(text or ''):
        if name not in seen:
            seen.append(name)
    return seen


def member_document(kit, name):
    """One of ``WEB_DOCUMENTS`` for the web service: the text ``docs NAME`` serves, its
    title, and, when it is a prompt document, the prompt and the values to replace."""
    if name not in WEB_DOCUMENTS:
        raise KeyError(name)
    text = read_kit_document(kit, name)
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith('# ')), name)
    prompt = prompt_of(text)
    return {'name': name, 'title': title, 'served_as': 'docs ' + name, 'file': DOCUMENTS[name], 'text': text,
            'prompt': prompt, 'placeholders': placeholders(prompt) if prompt else []}


def probe_endpoints(text, project, kit):
    """Warn about an onboarding document that names a project-restricted endpoint.

    The installed kit endpoint (``endpoint.py`` beside this module) serves every
    project. A project-specific wrapper refuses the others, and that refusal is
    visible in its own source, so this probe reads each Python path the document
    names and reports the ones carrying the guard. It is deterministic and
    offline: nothing is executed and no request is sent, so a server with no
    network still gets the warning. The caller warns and still installs the
    document; the probe never blocks or changes the write. A reference is reported
    by its resolved path, so a Windows 8.3 short name and its long form describe
    one file rather than hiding or duplicating it.
    """
    generic=(Path(kit).resolve()/'endpoint.py')
    warnings=[];seen=set()
    for reference in ENDPOINT_REFERENCE.findall(text):
        try:
            path=Path(reference).resolve()
        except (OSError,RuntimeError):
            continue  # not a usable path on this host
        if path==generic or path in seen:continue
        seen.add(path)
        try:
            source=path.read_text(encoding='utf-8-sig')
        except (OSError,UnicodeError,RuntimeError):
            continue  # not on this host, or not text: nothing deterministic to probe
        if ENDPOINT_REFUSAL in source.lower():
            warnings.append(f'WARNING: the onboarding document for {project} names endpoint {path}, '
                            f'which refuses projects other than its own ("{ENDPOINT_REFUSAL}"); workers must '
                            f'use the kit endpoint {generic} instead. The document was still installed; '
                            f'correct the endpoint and reinstall it.')
    return warnings

def execute(kit, project_path, project, actor, action, args, endpoint=None):
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError('Expected argument list')
    catalog = '\n'.join(['  docs --status  (every name as served, with its size, or as refused with its sentence)']
                       + ['  docs '+key for key in ['project', *DOCUMENTS]])
    if action == 'docs':
        if not args:return 'Installed onboarding documents (use your existing client prefix):\n'+catalog+'\n'
        if args == ['--status']:
            import json
            return json.dumps({'documents': catalog_status(kit, project_path)}, ensure_ascii=False, indent=2) + '\n'
        if len(args) != 1 or args[0] not in {'project', *DOCUMENTS}:
            raise ValueError('Unknown document; run docs for the fixed catalog')
        if args[0] == 'project':return read_document(project_path, 'ONBOARDING.md', PROJECT_LIMIT)
        return read_kit_document(kit, args[0])
    if action != 'onboard' or args:raise ValueError('Use onboard without arguments')
    # Read both before constructing output: never return a plausible but incomplete start.
    entry = read_document(project_path, 'ONBOARDING.md', PROJECT_LIMIT)
    # The start document is a file the kit ships (kittrial-5bb.153 item 1): over its
    # bound it is an installation fault, not something an operator can shorten.
    start = read_document(kit, DOCUMENTS['start'], START_LIMIT, kit_document='start')
    metadata = report(kit)
    in_use = Path(endpoint).resolve() if endpoint is not None else Path(kit).resolve()/'endpoint.py'
    return (f'# Orchestra onboarding\n\nProject: {project}\nSession actor: {actor}\n'
            f'Endpoint in use: {in_use}\n'
            f'{line(metadata)}\n\n'
            + start+'\n\n# Project entry point\n\n'+entry
            +'\n\n# Supporting documents\n\n'+catalog+'\n')
