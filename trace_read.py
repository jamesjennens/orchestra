"""kittrial-5bb.425: the trace read, answered from records alone (TRACEABILITY_DESIGN §5.2-§5.4).

A library: no I/O. The caller (endpoint.py `trace`) hands it the project's export rows, its operator and
verifier lists, the requirements-governance snapshot when a `req:` token needs one, the project's
current membership and the server's clock. Every view and every count is computed from ONE snapshot,
`Snapshot`, built by the same filter `brief` resolves pointers through (briefing.ContextPointers._load):
another project's rows, record anchors, merge slots and malformed rows are not work, and a caller-level
`exclude` hook removes any further row before anything is counted. So a row the caller may not read is
absent from every list and every total (hidden equals absent, §5.3 "permission filtering happens before
counts").

Views (§5.3): requirements, tasks, tokens, decisions, designs, round; plus `summary`, the counts the
Coverage panel shows. A project with no `design.*` entries answers the requirement, task, token and
decision views and says once that it has no designs (§5.4).
"""
import re
from datetime import datetime, timedelta, timezone

VIEWS = ('summary', 'requirements', 'tasks', 'tokens', 'decisions', 'designs', 'round')
PAGE_MAX = 50
ANSWER_MAX = 64 * 1024
RECENT_DAYS = 14
COMMENT_SCAN_MAX = 2000
ROUNDS_MAX = 50
LIST_MAX = 20
#: Cross-reviews shown per lane: LIST_MAX lanes of these must fit one answer (review b1).
CROSS_REVIEWS_MAX = 5
#: The distinct tokens checked per source, a task or a design section (review b3: a bound per source, so a planted
#: source hides only its own extra tokens; a global first-come bound let one task blind every later one).
TOKENS_PER_SOURCE_MAX = 200
#: A task's token fields, in the order they are read; each is bounded on its own (re-review N1).
FIELDS = ('description', 'checkpoint', 'contribution')
FIELD_OF = {'description': 'description', 'checkpoint-source': 'checkpoint', 'checkpoint-evidence': 'checkpoint',
            'contribution-summary': 'contribution'}
#: Room kept inside ANSWER_MAX for what is added after the page is cut: the generation and the route's cursor.
TAIL_RESERVE = 1024
SCOPE_RE = re.compile(r'[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*')
#: A row larger than the whole answer is replaced by this (review b1: the cap is hard even for one row).
OVERSIZED = 'This row is larger than the answer limit; it is not shown. Narrow the read (a scope) or ask the coordinator.'
TITLE_MAX = 120
WORK_TYPES = ('task', 'bug', 'feature', 'chore')
BAD_STATES = ('malformed', 'unsupported', 'conflicted', 'incomplete')
NO_DESIGNS = 'No designs in this project (no design.* entries).'
#: The fixed phrase list of the `possible-decision-in-comment` heuristic (the coordinator's Q5).
DECISION_PHRASES = ('we will go with', "let's go with", "we'll use", 'decided', 'i choose', 'go with option')
PUBLISHED = re.compile(r'Published drafts of ([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*) at (\S+)[ \t]*\Z')
CROSS_REVIEW = re.compile(r'Cross-review of ([A-Za-z0-9][A-Za-z0-9_.-]{0,160}) at ([0-9a-f]{7,40})\b')
SUPERSEDES = re.compile(r'Supersedes: decision:([A-Za-z0-9][A-Za-z0-9_.-]{0,160})[ \t]*\Z')
ROUND_TOKEN = re.compile(r'(?<![A-Za-z0-9_-])design-round:([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*)')
CHECKPOINT_PREFIX = 'Kind: task-checkpoint-v1\n'
CONTRIBUTION_PREFIX = 'Kind: contribution-review-v1\n'


def instant(value):
    """A tracker time as an aware datetime, or None; the export writes Z or an offset, with or without a fraction."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace('Z', '+00:00')
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:\d{2})?', text)
    if not match:
        return None
    fraction = (match.group(2) or '')[:6].ljust(6, '0') if match.group(2) else '000000'
    try:
        moment = datetime.fromisoformat('%s.%s%s' % (match.group(1).replace(' ', 'T'), fraction,
                                                     match.group(3) or '+00:00'))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc)


def stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ') if moment is not None else None


def label(value, limit=TITLE_MAX):
    from briefing import clip_context_label
    return clip_context_label(value, limit)


def first_line(text):
    return text.split('\n', 1)[0].rstrip('\r') if isinstance(text, str) else ''


def parent_of(row):
    for dependency in row.get('dependencies') or []:
        if isinstance(dependency, dict) and dependency.get('type') == 'parent-child':
            return dependency.get('depends_on_id')
    return None


def bounded(values, limit=LIST_MAX):
    values = list(values)
    return values[:limit], max(0, len(values) - limit)


class Snapshot:
    """What one caller may read of one project, and every derived index trace needs, built once.

    `members` is the project's current membership, {account: role}, as the service holds it; a host
    with no web service passes None, and then only listed operators may write the round's lines (Q7)
    and no comment is an owner's (Q5). `now` is the server's clock, injected so no rule reads the real
    one. `exclude(row)` removes a row before anything is read or counted: the one place a per-caller
    rule plugs in."""

    def __init__(self, rows, project, *, now, operators=None, verifiers=None, governance=None,
                 members=None, exclude=None, others=None, authority_state=None, journal_of=None):
        from briefing import ContextPointers
        self.project = project
        #: kittrial-5bb.449: ``others(project)`` gives the resolver of a project this caller may read, None for one
        #: it may not read or that does not exist (the same answer), or 'not-checked' past the bound of targets.
        self.others = others
        #: kittrial-5bb.454: the web state, read live, for who wrote a decision or an adoption (program_decisions).
        self.authority_state = authority_state
        #: kittrial-5bb.454 r2: ``journal_of(project)`` is the directory of a project's attestation journal, or None.
        self.journal_of = journal_of
        self.now = now
        self.operators = list(operators or [])
        self.verifiers = list(verifiers or [])
        self.members = dict(members or {})
        can_read = (lambda row: not exclude(row)) if exclude is not None else None
        self.pointers = ContextPointers(rows, project, self.operators, can_read=can_read, governance=governance)
        self.rows = self.pointers.loaded()
        self.by_id = {}
        for row in self.rows:
            self.by_id.setdefault(str(row.get('id')), []).append(row)
        # Work items: one row per id, never an anchor or a merge slot (the brief's own task index).
        self.items = {task: found[0] for task, found in self.pointers.tasks.items() if len(found) == 1}
        self.children = {}
        for task, row in sorted(self.items.items()):
            parent = parent_of(row)
            if parent is not None:
                self.children.setdefault(str(parent), []).append(task)
        self._resolved = {}
        self.truncated = {}
        self._sections = {}
        self.rounds_omitted = 0
        self.comments_scanned = None
        self._designs = None
        self._citations = None
        self._workflows = {}
        self._impact = None

    # -- the people whose lines count -------------------------------------------------------
    def owner(self, author):
        """A person owner in current membership (Q5): agents and other roles never count."""
        return isinstance(author, str) and self.members.get(author) == 'owner'

    def steward(self, author):
        """A person owner or coordinator in current membership, or a listed operator (Q7)."""
        return isinstance(author, str) and (self.members.get(author) in ('owner', 'coordinator')
                                            or author in self.operators)

    # -- pointers ----------------------------------------------------------------------------
    def resolve(self, pointer):
        """One resolution per distinct token; each requirement ledger is validated once (briefing._ledger). A token
        into another project (kittrial-5bb.449) is resolved in that project for this caller, or reads
        `not-visible`, the same row for a project that does not exist and one this caller may not read."""
        key = pointer['pointer']
        if key not in self._resolved:
            entry = self.pointers.resolve(pointer)
            if entry.get('state') == 'cross-project':
                entry = self._elsewhere(pointer, entry['project'])
            self._resolved[key] = entry
        return self._resolved[key]

    def _elsewhere(self, pointer, project):
        other = self.others(project) if self.others is not None else None
        if other is None or other == 'not-checked':
            return {'pointer': pointer['pointer'], 'kind': pointer['kind'], 'project': project,
                    'state': 'not-visible' if other is None else 'not-checked', 'trust': 'data', 'flags': []}
        entry = other.resolve(pointer)
        entry['project'] = project
        return entry

    def local(self, pointer):
        """Whether a token is this project's own (unqualified, or qualified with this project's id)."""
        from briefing import pointer_target
        return pointer_target(pointer, self.project)[0] == self.project

    def bounded_tokens(self, source, items, name=lambda item: item['pointer']):
        """`items` keeping only the first TOKENS_PER_SOURCE_MAX distinct tokens of one source (every occurrence of
        a kept token stays); how many distinct tokens were left out is recorded per source."""
        allowed, extra, kept = set(), set(), []
        for item in items:
            token = name(item)
            if token not in allowed:
                if len(allowed) >= TOKENS_PER_SOURCE_MAX:
                    extra.add(token)
                    continue
                allowed.add(token)
            kept.append(item)
        if extra:
            self.truncated[source] = len(extra)
        return kept

    def section_tokens(self, design, heading):
        """A design section's index tokens, parsed, bounded per section like a task's (review b3)."""
        key = (design['scope'], heading['anchor'])
        if key not in self._sections:
            from briefing import parse_context_pointers
            parsed = []
            for token in heading['tokens']:
                found = parse_context_pointers(token)
                if len(found) == 1:
                    parsed.append(found[0])
            self._sections[key] = self.bounded_tokens(('design', design['scope'], heading['anchor']), parsed)
        return self._sections[key]

    def requirement(self, key):
        return self.pointers.requirement(key)

    def adopts(self, row):
        """kittrial-5bb.454 (Q6): the state of a decision issue's Adopts: line, by the program view's rules. The program
        is read only where this caller may read it (.449's loader); otherwise not-visible, with nothing else."""
        import program_decisions
        found = program_decisions.lines(row)['adopts']
        if found is None:
            return None
        program = found[0]
        if program == self.project:
            program_rows = self.rows
        else:
            other = self.others(program) if self.others is not None else None
            if other == 'not-checked':
                return {'pointer': 'decision:%s/%s' % found[:2], 'state': 'not-checked'}
            program_rows = other.loaded() if other is not None else None
        journals = {name: self.journal_of(name) for name in (self.project, program)} if self.journal_of else None
        return program_decisions.trace_adopts(row, self.project, self.rows, program_rows, self.authority_state,
                                              self.operators, journals)

    def requirement_keys(self):
        return self.pointers.requirement_keys()

    # -- workflow (contributions) ------------------------------------------------------------
    def workflow(self, task):
        if task not in self._workflows:
            row = self.items[task]
            result = None
            if any(isinstance(c, dict) and isinstance(c.get('text'), str) and c['text'].startswith(CONTRIBUTION_PREFIX)
                   for c in row.get('comments') or []):
                from review_state import project as reviewed
                try:
                    result = reviewed(row, operators=self.operators)
                except (ValueError, TypeError, KeyError):
                    result = None
            self._workflows[task] = result
        return self._workflows[task]

    def delivered(self, task):
        """Q4: a task is delivered when it is closed or its current contribution is approved."""
        row = self.items[task]
        if row.get('status') == 'closed':
            return True
        flow = self.workflow(task)
        return bool(flow) and flow.get('review_state') == 'approved'

    # -- citations ---------------------------------------------------------------------------
    def sources(self, task):
        """(where, text) for every place a task may carry tokens (§2.4 "Where tokens go")."""
        from briefing import checkpoint_state
        row = self.items[task]
        found = [('description', row.get('description'))]
        try:
            current = checkpoint_state(row, normalize=False)['current']
        except (ValueError, TypeError, KeyError):
            current = None
        if current is not None:
            payload = current[0]
            for item in payload.get('open_items') or []:
                if isinstance(item, dict):
                    found.append(('checkpoint-source', item.get('source')))
            for item in payload.get('resolved') or []:
                if isinstance(item, dict):
                    found.append(('checkpoint-evidence', item.get('evidence')))
        flow = self.workflow(task)
        contribution = (flow or {}).get('contribution') if isinstance(flow, dict) else None
        if isinstance(contribution, dict):
            found.append(('contribution-summary', contribution.get('summary')))
        return found

    def citations(self):
        """{task: [(where, pointer)]} for the traceability kinds, over visible work items only."""
        if self._citations is None:
            from briefing import parse_context_pointers
            self._citations = {}
            for task in sorted(self.items):
                # Re-review N1: each field (description, then the current checkpoint, then the current
                # contribution) is its own source with its own bound, so one cannot leave another unchecked.
                fields = {}
                for where, text in self.sources(task):
                    if not isinstance(text, str):
                        continue
                    field = FIELD_OF.get(where, where)
                    for pointer in parse_context_pointers(text):
                        # kittrial-5bb.449: decision tokens are traceability kinds too (QUALIFIED_KINDS).
                        if pointer['kind'] in ('req', 'design', 'decision', 'iface'):
                            fields.setdefault(field, []).append((where, pointer))
                cited = []
                for field in FIELDS:
                    cited += self.bounded_tokens(('task', task, field), fields.get(field, []),
                                                 lambda item: item[1]['pointer'])
                self._citations[task] = cited
        return self._citations

    # -- designs -----------------------------------------------------------------------------
    def designs(self):
        """[{scope, key, view, record, shown, row, index, verification}] for every readable design entry."""
        if self._designs is None:
            import design_documents
            import reference_records
            self._designs = []
            entries, _ = reference_records.catalog(self.rows, self.operators, current=self.now.date())
            keys = {}
            for view in entries:
                if isinstance(view.get('key'), str) and view['key'].startswith(design_documents.DESIGN_KEY_PREFIX):
                    keys.setdefault(view['key'], []).append(view)
            trusted = reference_records._trusted(self.operators, self.verifiers)
            for key in sorted(keys):
                if len(keys[key]) != 1 or keys[key][0].get('state') in BAD_STATES:
                    continue
                view = keys[key][0]
                record = view.get('record')
                shown = record or view.get('proposed')
                if shown is None:
                    continue
                anchors = self.by_id.get(str(view.get('native_id'))) or []
                row = anchors[0] if len(anchors) == 1 else None
                authority = shown.get('authority') or {}
                index = verification = None
                if row is not None and design_documents.is_document_authority(authority):
                    index = design_documents.selected_index(row, key, authority)
                    if record is not None:
                        verification = design_documents.derive_verification(
                            design_documents.verifications_for(row, key, authority), trusted)
                self._designs.append({'scope': key[len(design_documents.DESIGN_KEY_PREFIX):], 'key': key,
                                      'view': view, 'record': record, 'shown': shown, 'index': index,
                                      'verification': verification})
        return self._designs

    def plan_epics(self):
        return {phase['epic'] for design in self.designs() if design['record'] is not None and design['index']
                for phase in design['index']['plan_phases'] if phase['epic']}

    # -- requirement impact (§5.3 tasks: requirement_impact.analyze) ------------------------
    def impact(self):
        """({task: affected requirement ids}, note): `analyze` over the work graph the tokens build.

        previous = the accepted revisions tasks cite, current = each requirement's current accepted
        revision. A cited revision that is a draft, newer than the current one or unresolvable is the
        tokens view's business and never reaches `analyze` (its unknown-revision refusal, requirement_
        impact.py:80, would otherwise fail the whole read)."""
        if self._impact is not None:
            return self._impact
        from export_requirements import exact_reference
        current, cited = {}, {}
        for task, pointers in self.citations().items():
            for _, pointer in pointers:
                if pointer['kind'] != 'req' or not self.local(pointer):
                    continue
                entry = self.resolve(pointer)
                reference = entry.get('reference')
                if reference is None or 'draft' in entry.get('flags', []):
                    continue
                cited.setdefault(task, {})
                prior = cited[task].get(reference['id'])
                if prior is None or reference['revision'] > prior['revision']:
                    cited[task][reference['id']] = reference
        keys = {}
        for key in self.requirement_keys():
            entry = self.requirement(key)
            reference = entry.get('reference')
            if reference is not None and entry['state'] == 'live' and not entry.get('flags'):
                current[reference['id']] = reference
                keys[reference['id']] = key
        # The resolver's own validated ledgers, read once per requirement per snapshot (review b2).
        ledgers = {}
        for key in self.requirement_keys():
            ledger = self.pointers.ledger(key)
            if isinstance(ledger, tuple):
                ledgers[ledger[0]['id']] = ledger[1]

        def record(reference):
            return ledgers.get(reference['id'], {}).get(reference['revision'])

        layers = []
        for task in sorted(cited):
            for rid, reference in sorted(cited[task].items()):
                if rid in current and reference['revision'] > current[rid]['revision']:
                    del cited[task][rid]
                    continue
                for layer in layers:
                    if layer.get(rid, reference) == reference:
                        layer[rid] = reference
                        break
                else:
                    layers.append({rid: reference})
        nodes = [{'id': task, 'kind': 'task', 'requirements': sorted(refs.values(), key=lambda r: r['id']),
                  'depends_on': [], 'evidence': []} for task, refs in sorted(cited.items()) if refs]
        if not nodes or not current:
            self._impact = ({}, 'nothing to analyze')
            return self._impact
        try:
            from requirement_impact import analyze

            def manifest(name, references):
                from requirements import content_hash
                body = {'schema_version': 1, 'baseline': name, 'state': 'draft',
                        'canonical_project': str(self.project), 'job': 'trace', 'authority': 'trace-read',
                        'hash_convention': 'sha256-canonical-json-v1', 'narrative': [],
                        'requirements': [record(references[rid]) for rid in sorted(references)]}
                if any(item is None for item in body['requirements']):
                    raise ValueError('a cited revision has no stored record')
                for reference in references.values():
                    exact_reference(reference)
                body['sha256'] = content_hash(body)
                return body
            now = manifest('trace-current', current)
            older = [manifest('trace-cited-%d' % number, layer) for number, layer in enumerate(layers)]
            result = analyze(older[0], now, {'schema_version': 1, 'nodes': nodes},
                             history=[{'manifest': item, 'acceptance': None} for item in older[1:]])
        except (ValueError, TypeError, KeyError) as error:
            self._impact = ({}, 'impact analysis unavailable: %s' % str(error)[:200])
            return self._impact
        affected = {node['id']: [keys.get(rid, rid) for rid in node['affected_requirements']]
                    for node in result['nodes'] if node['state'] == 'needs-reassessment'}
        self._impact = (affected, 'analyzed %d tasks' % len(nodes))
        return self._impact


# -- views ------------------------------------------------------------------------------------
def requirements_view(snap):
    designs = snap.designs()
    has_designs = bool(designs)
    citing_designs = {}
    for design in designs:
        if design['record'] is None:
            continue
        keys = set()
        for item in design['index']['front']['requirements'] if design['index'] else []:
            keys.add(item.split('@', 1)[0])
        for heading in design['index']['headings'] if design['index'] else []:
            for token in heading['tokens']:
                key = local_req_key(token, snap.project)
                if key is not None:
                    keys.add(key)
        for key in keys:
            citing_designs.setdefault(key, set()).add(design['scope'])
    citing_tasks = {}
    for task, pointers in snap.citations().items():
        for _, pointer in pointers:
            key = local_req_key(pointer['pointer'], snap.project)
            if key is not None:
                citing_tasks.setdefault(key, set()).add(task)
    import capability_records
    capabilities = {}
    entries, _ = capability_records.catalog(snap.rows, snap.operators)
    for view in entries:
        record = view.get('record')
        if view.get('state') in BAD_STATES or not isinstance(record, dict):
            continue
        for link in record.get('requirements') or []:
            if isinstance(link, dict) and isinstance(link.get('key'), str):
                capabilities.setdefault(link['key'], set()).add(record.get('key') or view.get('key'))
    rows = []
    for key in snap.requirement_keys():
        entry = snap.requirement(key)
        if entry['state'] != 'live' or entry.get('flags'):
            continue   # each ACCEPTED requirement, current and in force (§5.3)
        tasks = sorted(citing_tasks.get(key, ()))
        flags = []
        if has_designs and not citing_designs.get(key):
            flags.append('no-design')
        if not tasks:
            flags.append('no-task')
        if not capabilities.get(key) and any(snap.delivered(task) for task in tasks):
            flags.append('no-capability')
        shown_tasks, more_tasks = bounded(tasks)
        shown_designs, more_designs = bounded(sorted(citing_designs.get(key, ())))
        shown_capabilities, more_capabilities = bounded(sorted(capabilities.get(key, ())))
        rows.append({'key': key, 'reference': entry['reference'], 'title': entry.get('label'),
                     'designs': shown_designs, 'designs_omitted': more_designs, 'tasks': shown_tasks,
                     'tasks_omitted': more_tasks, 'capabilities': shown_capabilities,
                     'capabilities_omitted': more_capabilities, 'flags': flags})
    return rows


def tasks_view(snap):
    affected, _ = snap.impact()
    epics = snap.plan_epics()
    since = snap.now - timedelta(days=RECENT_DAYS)
    rows = []
    for task in sorted(snap.items):
        row = snap.items[task]
        if row.get('issue_type') not in WORK_TYPES or 'requirement' in (row.get('labels') or []):
            continue
        if row.get('status') == 'closed':
            closed = instant(row.get('closed_at'))
            if closed is None or closed < since:
                continue
        cited = snap.citations()[task]
        flags = []
        if task in affected:
            flags.append('cites-superseded-revision')
        described = [p for where, p in cited if where == 'description']
        if parent_of(row) in epics and not described:
            flags.append('cites-nothing')
        shown, more = bounded(cited)
        rows.append({'id': task, 'title': label(row.get('title')), 'status': row.get('status'),
                     'cites': [cite(snap, where, p) for where, p in shown],
                     'cites_omitted': more, 'tokens_not_checked': sum(snap.truncated.get(('task', task, field), 0) for field in FIELDS),
                     'affected_requirements': affected.get(task, [])[:LIST_MAX],
                     'affected_requirements_omitted': max(0, len(affected.get(task, [])) - LIST_MAX),
                     'flags': flags})
    return rows


def cite(snap, where, pointer):
    entry = snap.resolve(pointer)
    shown = dict(pointer=pointer['pointer'], where=where, state=entry['state'], flags=entry.get('flags', []))
    if entry.get('project') is not None:
        shown['project'] = entry['project']
    return shown


def local_req_key(token, project):
    """The requirement key of a `req:` token that is this project's own, else None (kittrial-5bb.449: a token into
    another project never counts as citing a key here)."""
    from briefing import parse_context_pointers, pointer_target
    found = parse_context_pointers(token) if isinstance(token, str) else []
    if len(found) != 1 or found[0]['kind'] != 'req':
        return None
    where, value = pointer_target(found[0], project)
    return value.split('@', 1)[0] if where == project else None


def unresolved(entry):
    flags = list(entry.get('flags') or [])
    if entry['state'] not in ('live',) or flags:
        return flags or [entry['state']]
    return []


def tokens_view(snap):
    rows = []
    for task, pointers in snap.citations().items():
        for where, pointer in pointers:
            entry = snap.resolve(pointer)
            flags = unresolved(entry)
            if flags:
                rows.append(dict({'pointer': pointer['pointer'], 'where': {'task': task, 'field': where}, 'flags': flags},
                                 **({'project': entry['project']} if entry.get('project') is not None else {})))
    for design in snap.designs():
        for heading in design['index']['headings'] if design['index'] else []:
            for pointer in snap.section_tokens(design, heading):
                entry = snap.resolve(pointer)
                flags = unresolved(entry)
                if flags:
                    rows.append(dict({'pointer': pointer['pointer'],
                                      'where': {'design': design['scope'], 'section': heading['anchor']}, 'flags': flags},
                                     **({'project': entry['project']} if entry.get('project') is not None else {})))
    # kittrial-5bb.449, the `/` lint: a key of this project containing `/` is cited qualified, even here.
    for key in snap.requirement_keys():
        if '/' in key:
            rows.append({'pointer': None, 'where': {'requirement': key}, 'flags': ['qualify-key'],
                         'cite_as': 'req:%s/%s' % (snap.project, key)})
    # Review b3: a source over the bound says so in one row; only its own extra tokens go unchecked.
    for source, count in sorted(snap.truncated.items()):
        where = {'task': source[1], 'field': source[2]} if source[0] == 'task' else {'design': source[1], 'section': source[2]}
        rows.append({'pointer': None, 'where': where, 'flags': ['tokens-not-checked'], 'omitted': count})
    return rows


def decisions_view(snap):
    """Decision issues and the heuristic `possible-decision-in-comment`, over a bounded comment scan."""
    from delegations import is_decision_issue
    designs = snap.designs()
    rows = []
    cited_comments = set()
    decisions = []
    for task in sorted(snap.items):
        row = snap.items[task]
        if not is_decision_issue(row):
            continue
        decisions.append(task)
        description = row.get('description') if isinstance(row.get('description'), str) else ''
        for match in re.finditer(r'(?<![A-Za-z0-9_-])comment:([A-Za-z0-9._-]{1,200})', description):
            cited_comments.add(match.group(1))
        supersedes = [m.group(1) for m in (SUPERSEDES.fullmatch(line.rstrip('\r'))
                                           for line in description.split('\n')[:1]) if m]
        cited_by = sorted(d['scope'] for d in designs
                          if task in (((d['shown'] or {}).get('decisions')) or [])
                          or (d['index'] and task in d['index']['front']['decisions_open']))
        shown_designs, more_designs = bounded(cited_by)
        rows.append({'kind': 'decision', 'id': task, 'title': label(row.get('title')), 'status': row.get('status'),
                     'supersedes': supersedes, 'designs': shown_designs, 'designs_omitted': more_designs, 'flags': []})
        adopts = snap.adopts(row)
        if adopts is not None:
            rows[-1]['adopts'] = adopts
    # Q5: owners' prose on work items; a structured record ("Kind: ...") is never prose. The owner filter comes
    # BEFORE the bound (review note 4), so nobody else's comments can push an owner's out of the scan.
    scan = []
    for task in sorted(snap.items):
        row = snap.items[task]
        if is_decision_issue(row):
            continue
        for comment in row.get('comments') or []:
            if isinstance(comment, dict) and isinstance(comment.get('text'), str) \
                    and not comment['text'].startswith('Kind: ') and snap.owner(comment.get('author')):
                scan.append((row, comment))
    total = len(scan)
    scan.sort(key=lambda item: (instant(item[1].get('created_at')) or datetime.min.replace(tzinfo=timezone.utc),
                                str(item[1].get('id'))), reverse=True)
    for row, comment in scan[:COMMENT_SCAN_MAX]:
        if str(comment.get('id')) in cited_comments:
            continue
        lowered = comment['text'].lower()
        if any(phrase in lowered for phrase in DECISION_PHRASES):
            rows.append({'kind': 'possible-decision', 'id': str(comment.get('id')), 'task': str(row.get('id')),
                         'text': label(comment['text']), 'at': comment.get('created_at'),
                         'flags': ['possible-decision-in-comment'], 'heuristic': True})
    snap.comments_scanned = (min(total, COMMENT_SCAN_MAX), total)
    return rows


def designs_view(snap):
    rows = []
    for design in snap.designs():
        record, index, verification = design['record'], design['index'], design['verification']
        flags = []
        if record is None:
            flags.append('not-selected')
        elif verification is not None and verification['state'] != 'verified':
            flags.append(verification['state'])
        if index is None:
            flags.append('no-index')
        front_status = index['front']['status'] if index else None
        if index is not None and (record is not None) != (front_status == 'selected'):
            flags.append('front-status-disagrees')
        if design['view'].get('due') == 'expired':
            flags.append('review-by-past')
        sections, phases_without_epic, epics_without_task, cited, covered = [], [], [], set(), set()
        if index is not None:
            from briefing import parse_context_pointers
            phase_keys = set()
            for phase in index['plan_phases']:
                for line in phase['requirements']:
                    for pointer in parse_context_pointers(line):
                        key = local_req_key(pointer['pointer'], snap.project)
                        if key is not None:
                            phase_keys.add(key)
                if phase['epic'] is None:
                    phases_without_epic.append(phase['anchor'])
                elif not snap.children.get(phase['epic']):
                    epics_without_task.append(phase['epic'])
            planned = {phase['anchor'] for phase in index['plan_phases']}
            for heading in index['headings']:
                keys = {key for key in (local_req_key(token, snap.project) for token in heading['tokens'])
                        if key is not None}
                cited |= keys
                if heading['anchor'] in planned or heading['anchor'] == 'plan':
                    continue
                if keys and not keys & phase_keys:
                    sections.append(heading['anchor'])
            covered = cited & phase_keys
        for name, values in (('section-without-plan-phase', sections), ('plan-phase-without-epic', phases_without_epic),
                             ('epic-without-task', epics_without_task)):
            if values:
                flags.append(name)
        shown = design['shown']
        rows.append({'scope': design['scope'], 'key': design['key'], 'title': label(shown.get('title')),
                     'selected': record is not None, 'revision': shown.get('revision'),
                     'state': verification['state'] if verification else ('not-selected' if record is None else None),
                     'front_status': front_status, 'review_by': shown.get('review_by'),
                     'sections_without_plan_phase': sections[:LIST_MAX],
                     'sections_without_plan_phase_omitted': max(0, len(sections) - LIST_MAX),
                     'plan_phases_without_epic': phases_without_epic[:LIST_MAX],
                     'plan_phases_without_epic_omitted': max(0, len(phases_without_epic) - LIST_MAX),
                     'epics_without_task': epics_without_task[:LIST_MAX],
                     'epics_without_task_omitted': max(0, len(epics_without_task) - LIST_MAX),
                     'requirements': {'cited': len(cited), 'planned': len(covered)}, 'flags': flags})
    return rows


def round_view(snap, scope=None):
    """Each design round (an epic carrying `design-round:<scope>`): lane times side by side (§4.1 step 3)."""
    from briefing import checkpoint_state
    designs = {design['scope']: design for design in snap.designs()}
    rows = []
    for task in sorted(snap.items):
        row = snap.items[task]
        if row.get('issue_type') != 'epic':
            continue
        found = ROUND_TOKEN.search('%s\n%s' % (row.get('title') or '', row.get('description') or ''))
        if not found or (scope is not None and found.group(1) != scope):
            continue
        if len(rows) >= ROUNDS_MAX:
            snap.rounds_omitted += 1   # counted, so the summary can say its round count is partial
            continue
        name = found.group(1)
        published = None
        for comment in row.get('comments') or []:
            if not isinstance(comment, dict) or not snap.steward(comment.get('author')):
                continue   # Q7: only a person owner, a coordinator or a listed operator moves this time
            match = PUBLISHED.fullmatch(first_line(comment.get('text')))
            moment = instant(match.group(2)) if match and match.group(1) == name else None
            if moment is not None and (published is None or moment < published):
                published = moment
        lanes = []
        for lane in snap.children.get(task, []):
            lane_row = snap.items[lane]
            plan = delivery = None
            try:
                history = checkpoint_state(lane_row, normalize=False)['history']
            except (ValueError, TypeError, KeyError):
                history = []
            if history:
                first = next((c for c in lane_row.get('comments') or []
                              if isinstance(c, dict) and str(c.get('id')) == str(history[0][0])), None)
                plan = instant((first or {}).get('created_at'))
            flow = snap.workflow(lane)
            if isinstance(flow, dict):
                times = [instant(item.get('timestamp')) for item in [flow.get('contribution') or {}]
                         + list(flow.get('prior_contributions') or [])]
                times = [moment for moment in times if moment is not None]
                delivery = min(times) if times else None
            reviews = []
            for comment in lane_row.get('comments') or []:
                match = CROSS_REVIEW.match(first_line((comment or {}).get('text')))
                if match:
                    # Review note 3: whose line it is, as data (any member may write one).
                    reviews.append({'of': match.group(1), 'commit': match.group(2), 'by': label(comment.get('author')),
                                    'at': stamp(instant(comment.get('created_at')))})
            flags = ['delivered-after-publication'] if published and delivery and delivery > published else []
            shown_reviews, more_reviews = bounded(reviews, CROSS_REVIEWS_MAX)
            lanes.append({'task': lane, 'title': label(lane_row.get('title')), 'plan_at': stamp(plan),
                          'delivered_at': stamp(delivery), 'cross_reviews': shown_reviews,
                          'cross_reviews_omitted': more_reviews, 'flags': flags})
        design = designs.get(name)
        converged = None
        if design is not None:
            # The converged design and its decision are read from the catalog entry, never from comments.
            converged = {'key': design['key'], 'selected': design['record'] is not None,
                         'revision': design['shown'].get('revision'),
                         'decisions': list((design['shown'].get('decisions') or [])[:LIST_MAX]),
                         'decisions_omitted': max(0, len(design['shown'].get('decisions') or []) - LIST_MAX)}
        # Review b1: the lanes are bounded like every list; the round's flags still count every lane.
        shown_lanes, more_lanes = bounded(lanes)
        rows.append({'epic': task, 'scope': name, 'title': label(row.get('title')),
                     'published_at': stamp(published),
                     'publication': 'recorded' if published else 'publication not recorded',
                     'lanes': shown_lanes, 'lanes_omitted': more_lanes,
                     'late_lanes': sum(bool(lane['flags']) for lane in lanes), 'converged': converged,
                     'flags': sorted({flag for lane in lanes for flag in lane['flags']})})
    return rows


BUILDERS = {'requirements': requirements_view, 'tasks': tasks_view, 'tokens': tokens_view,
            'decisions': decisions_view, 'designs': designs_view}


def summary_view(snap):
    """The Coverage panel's lines: counts only, from the same snapshot as every view. A line whose count a bound cut
    short says `partial: true` (review b3), with the reason in the coverage line."""
    has_designs = bool(snap.designs())
    requirements = requirements_view(snap)
    tokens = tokens_view(snap)
    cut = bool(snap.truncated)
    lines = [{'view': 'requirements', 'total': len(requirements), 'partial': cut,
              'counts': {flag: sum(flag in row['flags'] for row in requirements)
                         for flag in ('no-task', 'no-capability') + (('no-design',) if has_designs else ())}},
             {'view': 'tokens', 'total': len(tokens), 'partial': cut,
              'counts': {'tokens-not-checked': sum(row.get('omitted', 0) for row in tokens)}},
             pointers_line(snap)]
    if has_designs:
        tasks = tasks_view(snap)
        decisions = decisions_view(snap)
        designs = designs_view(snap)
        rounds = round_view(snap)
        scanned = snap.comments_scanned or (0, 0)
        lines += [{'view': 'tasks', 'total': len(tasks), 'partial': cut,
                   'counts': {flag: sum(flag in row['flags'] for row in tasks)
                              for flag in ('cites-superseded-revision', 'cites-nothing')}},
                  {'view': 'decisions', 'total': sum(row['kind'] == 'decision' for row in decisions),
                   'partial': scanned[0] < scanned[1],
                   'counts': {'possible-decision-in-comment': sum(row['kind'] != 'decision' for row in decisions)}},
                  {'view': 'designs', 'total': len(designs), 'partial': False,
                   'counts': {flag: sum(flag in row['flags'] for row in designs)
                              for flag in ('not-selected', 'unverified', 'design-drift', 'section-without-plan-phase',
                                           'plan-phase-without-epic', 'epic-without-task')}},
                  {'view': 'round', 'total': len(rounds) + snap.rounds_omitted, 'partial': bool(snap.rounds_omitted),
                   'counts': {'delivered-after-publication': sum(bool(row['flags']) for row in rounds)}}]
    return lines


def all_pointers(snap):
    """Every distinct token the views read: the tasks' and the designs' sections'."""
    seen = {}
    for _task, pointers in snap.citations().items():
        for _where, pointer in pointers:
            seen.setdefault(pointer['pointer'], pointer)
    for design in snap.designs():
        for heading in design['index']['headings'] if design['index'] else []:
            for pointer in snap.section_tokens(design, heading):
                seen.setdefault(pointer['pointer'], pointer)
    return list(seen.values())


def pointers_line(snap):
    """kittrial-5bb.449: resolved, broken, not visible to this caller, and not checked (past the bound of other
    projects). A token another reader may resolve is never called broken."""
    counts = {'resolved': 0, 'broken': 0, 'not-visible': 0, 'not-checked': 0}
    for pointer in all_pointers(snap):
        entry = snap.resolve(pointer)
        if entry['state'] in ('not-visible', 'not-checked'):
            counts[entry['state']] += 1
        elif entry['state'] == 'unchecked':
            continue
        else:
            counts['broken' if unresolved(entry) else 'resolved'] += 1
    return {'view': 'pointers', 'total': sum(counts.values()), 'partial': bool(snap.truncated), 'counts': counts}


def coverage(snap):
    designs = snap.designs()
    selected = [d for d in designs if d['record'] is not None]
    states = [d['verification']['state'] for d in selected if d['verification']]
    from coordination import is_merge_slot
    block = {'rows_read': sum(not is_merge_slot(row) for row in snap.rows), 'work_items': len(snap.items),
             'designs': len(designs), 'designs_indexed': sum(d['index'] is not None for d in designs),
             'unverified': states.count('unverified'), 'design_drift': states.count('design-drift')}
    scanned = getattr(snap, 'comments_scanned', None)
    if scanned is not None:
        block['comments_scanned'] = {'scanned': scanned[0], 'total': scanned[1], 'limit': COMMENT_SCAN_MAX}
    if snap._impact is not None:
        block['impact'] = snap._impact[1]
    parts = ['%d designs indexed' % block['designs_indexed']]
    if block['unverified']:
        parts.append('%d unverified' % block['unverified'])
    if block['design_drift']:
        parts.append('%d drifted' % block['design_drift'])
    text = '%s; %d rows read' % (', '.join(parts), block['rows_read'])
    if scanned is not None:
        text += '; possible decisions: the newest %d of %d owner comments scanned' % scanned
    if snap.rounds_omitted:
        block['rounds_omitted'] = snap.rounds_omitted
        text += '; %d more design rounds not shown (the limit is %d)' % (snap.rounds_omitted, ROUNDS_MAX)
    if snap.truncated:
        block['tokens_not_checked'] = {'sources': len(snap.truncated), 'tokens': sum(snap.truncated.values()),
                                       'limit_per_source': TOKENS_PER_SOURCE_MAX}
        text += '; %d sources carry more than %d distinct tokens: %d of their tokens are not checked' % (
            len(snap.truncated), TOKENS_PER_SOURCE_MAX, sum(snap.truncated.values()))
    block['text'] = text
    return block


def trace(snap, view, *, scope=None, offset=0, limit=PAGE_MAX):
    """One page of one view. `offset` comes from the caller's own cursor; the answer stays under ANSWER_MAX."""
    if view not in VIEWS:
        raise ValueError('trace views: %s' % ', '.join(VIEWS))
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= PAGE_MAX:
        raise ValueError('trace pages: an offset of 0 or more and a limit of 1-%d' % PAGE_MAX)
    has_designs = bool(snap.designs())
    answer = {'schema': 'trace-v1', 'view': view, 'trust': 'data', 'designs': has_designs}
    if not has_designs:
        answer['note'] = NO_DESIGNS
    if view == 'summary':
        rows = summary_view(snap)
    elif view == 'round':
        rows = round_view(snap, scope) if has_designs or scope else []
    elif view == 'designs' and not has_designs:
        rows = []
    else:
        rows = BUILDERS[view](snap)
    answer['total'] = len(rows)
    page = rows[offset:offset + limit]
    answer['coverage'] = coverage(snap)
    from briefing import encoded_size
    import json
    def fits():
        # Review note 7: the generation and the route's cursor are added after this cut; their room is kept here.
        return encoded_size(json.dumps(answer, ensure_ascii=False)) + TAIL_RESERVE <= ANSWER_MAX
    while True:
        answer['rows'] = page
        answer['next_offset'] = offset + len(page) if offset + len(page) < len(rows) else None
        if len(page) <= 1 or fits():
            break
        page = page[:max(1, len(page) // 2)]
    if page and not fits():
        # Review b1: the cap is hard even for one row. The row keeps only what names it, with a plain note.
        answer['rows'] = [oversized(page[0])]
        if not fits():
            answer['rows'] = [{'oversized': True, 'note': OVERSIZED}]
    return answer


def oversized(row):
    """A row too large to send: its identifying fields, clipped, and OVERSIZED."""
    stub = {'oversized': True, 'note': OVERSIZED}
    for name in ('view', 'kind', 'id', 'key', 'scope', 'epic', 'task', 'pointer'):
        value = row.get(name) if isinstance(row, dict) else None
        if isinstance(value, str):
            stub[name] = label(value, 200)['text']
    return stub


STALE = 'The project changed since this page was read; start again from the first page.'
USAGE = 'trace VIEW [--scope SCOPE] [--offset N] [--limit N] [--generation G]; views: ' + ', '.join(VIEWS)


def parse_options(args):
    """`trace VIEW [--scope S] [--offset N] [--limit N] [--generation G]`: the endpoint action's arguments."""
    if not isinstance(args, list) or any(not isinstance(x, str) or '\0' in x for x in args):
        raise ValueError('Expected argument list')
    rest = [token for token in args if token != '--json']
    if not rest or rest[0] not in VIEWS:
        raise ValueError(USAGE)
    options = {'view': rest[0], 'scope': None, 'offset': 0, 'limit': PAGE_MAX, 'generation': None}
    pairs, seen = rest[1:], set()
    if len(pairs) % 2:
        raise ValueError(USAGE)
    for flag, value in zip(pairs[0::2], pairs[1::2]):
        name = flag[2:] if flag.startswith('--') else None
        if name not in ('scope', 'offset', 'limit', 'generation') or name in seen:
            raise ValueError(USAGE)
        seen.add(name)
        if name in ('offset', 'limit'):
            if not re.fullmatch(r'0|[1-9][0-9]{0,8}', value):
                raise ValueError(USAGE)
            options[name] = int(value)
        elif name == 'scope':
            if len(value) > 73 or not SCOPE_RE.fullmatch(value):
                raise ValueError(USAGE)
            options['scope'] = value
        else:
            if not re.fullmatch(r'[A-Za-z0-9:_-]{1,80}', value):
                raise ValueError(USAGE)
            options['generation'] = value
    return options


def generation(snap):
    """The generation a page was read at: a digest of the caller's FILTERED snapshot (review note 2), so a change
    to a row the caller cannot read (another project's, an excluded one, the merge slot) never moves it."""
    import hashlib
    import json
    from coordination import is_merge_slot
    visible = [row for row in snap.rows if not is_merge_slot(row)]
    return 's:' + hashlib.sha256(json.dumps(visible, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:32]


def in_project(row, project):
    """The snapshot's own row rule (briefing.ContextPointers._load): this project's, readable rows. Review note 2:
    whether the governance file is read is decided from these, never from another project's rows."""
    return isinstance(row, dict) and row.get('project_id', project) == project and not row.get('malformed')


def read_governance(rows, journal, project):
    """The requirements-governance snapshot, read only when the export holds a requirement row (as `brief` reads it
    only for a `req:` token); None when there is none or it cannot be read. The one file read of a trace answer."""
    if journal is None or not any(in_project(row, project) and 'requirement' in (row.get('labels') or [])
                                  for row in rows):
        return None
    import requirement_governance
    try:
        return requirement_governance.snapshot(journal, project)
    except (ValueError, OSError):
        return None


def answer(rows, project, options, *, operators=None, verifiers=None, members=None, governance=None, now=None,
           others=None, authority_state=None, journal_of=None):
    """One page for the endpoint action, from rows and state already read: no I/O, so the caller builds it after
    releasing the project lock (review b2). A page asked for at another generation is refused. ``others`` reads the
    projects that this page's qualified tokens name, each under its own lock, only where the caller may read it
    (kittrial-5bb.449)."""
    snap = Snapshot(rows, project, now=snapshot_now(now), operators=operators, verifiers=verifiers,
                    governance=governance, members=members, others=others, authority_state=authority_state,
                    journal_of=journal_of)
    current = generation(snap)
    if options['generation'] is not None and options['generation'] != current:
        raise ValueError(STALE)
    result = trace(snap, options['view'], scope=options['scope'], offset=options['offset'], limit=options['limit'])
    result['generation'] = current
    return result


#: At most this many other projects are read for one trace page (PROGRAM_DESIGN §3.2, My work's bound).
TARGETS_MAX = 50
#: The web service's own argument: the projects its caller may read (kittrial-5bb.449). Any other caller is refused.
READABLE_FLAG = '--readable'
READABLE_REFUSED = ('trace: --readable is the web service\'s own argument, for the projects its caller may read; '
                    'a host key reads the projects it is bound to')


def split_readable(args):
    """``(the other arguments, the set given with --readable or None)``: the web service passes one --readable per
    project its caller may read, or a lone --readable for none."""
    rest, readable, index = [], None, 0
    while index < len(args):
        if args[index] == READABLE_FLAG:
            readable = set() if readable is None else readable
            if index + 1 < len(args) and not args[index + 1].startswith('--'):
                readable.add(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        rest.append(args[index])
        index += 1
    return rest, readable


def others_loader(readable, read_project, limit=TARGETS_MAX):
    """A Snapshot's ``others`` (kittrial-5bb.449). ``readable`` is the set of projects this caller may read (None:
    any, an operator's unbound host key); ``read_project(project)`` reads one, under its own lock, and gives its
    ContextPointers, or None when there is no such project. A project outside ``readable`` is never read and reads
    exactly like one that does not exist. At most ``limit`` distinct projects are taken; past that, 'not-checked'."""
    cache, asked = {}, []

    def load(project):
        if project not in cache:
            if len(asked) >= limit:
                return 'not-checked'
            asked.append(project)
            cache[project] = read_project(project) if readable is None or project in readable else None
        return cache[project]
    load.asked = asked
    return load


def snapshot_now(value=None):
    """The server's clock as an aware datetime; tests pass a fixed one."""
    return value if value is not None else datetime.now(timezone.utc)
