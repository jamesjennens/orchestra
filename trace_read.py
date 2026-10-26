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
                 members=None, exclude=None):
        from briefing import ContextPointers
        self.project = project
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
        key = pointer['pointer']
        if key not in self._resolved:
            self._resolved[key] = self.pointers.resolve(pointer)
        return self._resolved[key]

    def requirement(self, key):
        return self.pointers.requirement(key)

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
                cited = []
                for where, text in self.sources(task):
                    if not isinstance(text, str):
                        continue
                    for pointer in parse_context_pointers(text):
                        if pointer['kind'] in ('req', 'design'):
                            cited.append((where, pointer))
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
        import requirement_records as records
        current, cited = {}, {}
        for task, pointers in self.citations().items():
            for _, pointer in pointers:
                if pointer['kind'] != 'req':
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
        records_by = {}

        def record(reference):
            token = (reference['id'], reference['revision'])
            if token not in records_by:
                rows = self.by_id.get(reference['id']) or []
                revisions = records.existing_revisions(rows[0]) if len(rows) == 1 else {}
                records_by[token] = revisions.get(reference['revision'])
            return records_by[token]

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
                if token.startswith('req:'):
                    keys.add(token[4:].split('@', 1)[0])
        for key in keys:
            citing_designs.setdefault(key, set()).add(design['scope'])
    citing_tasks = {}
    for task, pointers in snap.citations().items():
        for _, pointer in pointers:
            if pointer['kind'] == 'req':
                citing_tasks.setdefault(pointer['value'].split('@', 1)[0], set()).add(task)
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
        rows.append({'key': key, 'reference': entry['reference'], 'title': entry.get('label'),
                     'designs': sorted(citing_designs.get(key, ())), 'tasks': shown_tasks,
                     'tasks_omitted': more_tasks, 'capabilities': sorted(capabilities.get(key, ()))[:LIST_MAX],
                     'flags': flags})
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
                     'cites': [dict(pointer=p['pointer'], where=where, state=snap.resolve(p)['state'],
                                    flags=snap.resolve(p).get('flags', [])) for where, p in shown],
                     'cites_omitted': more, 'affected_requirements': affected.get(task, [])[:LIST_MAX],
                     'flags': flags})
    return rows


def unresolved(entry):
    flags = list(entry.get('flags') or [])
    if entry['state'] not in ('live',) or flags:
        return flags or [entry['state']]
    return []


def tokens_view(snap):
    rows = []
    for task, pointers in snap.citations().items():
        for where, pointer in pointers:
            flags = unresolved(snap.resolve(pointer))
            if flags:
                rows.append({'pointer': pointer['pointer'], 'where': {'task': task, 'field': where}, 'flags': flags})
    from briefing import parse_context_pointers
    for design in snap.designs():
        for heading in design['index']['headings'] if design['index'] else []:
            for token in heading['tokens']:
                parsed = parse_context_pointers(token)
                if len(parsed) != 1:
                    continue
                flags = unresolved(snap.resolve(parsed[0]))
                if flags:
                    rows.append({'pointer': token, 'where': {'design': design['scope'], 'section': heading['anchor']},
                                 'flags': flags})
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
        rows.append({'kind': 'decision', 'id': task, 'title': label(row.get('title')), 'status': row.get('status'),
                     'supersedes': supersedes, 'designs': cited_by, 'flags': []})
    # Q5: owners' prose on work items; a structured record ("Kind: ...") is never prose.
    scan = []
    for task in sorted(snap.items):
        row = snap.items[task]
        if is_decision_issue(row):
            continue
        for comment in row.get('comments') or []:
            if isinstance(comment, dict) and isinstance(comment.get('text'), str) \
                    and not comment['text'].startswith('Kind: '):
                scan.append((row, comment))
    total = len(scan)
    scan.sort(key=lambda item: (instant(item[1].get('created_at')) or datetime.min.replace(tzinfo=timezone.utc),
                                str(item[1].get('id'))), reverse=True)
    for row, comment in scan[:COMMENT_SCAN_MAX]:
        if not snap.owner(comment.get('author')) or str(comment.get('id')) in cited_comments:
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
                        if pointer['kind'] == 'req':
                            phase_keys.add(pointer['value'].split('@', 1)[0])
                if phase['epic'] is None:
                    phases_without_epic.append(phase['anchor'])
                elif not snap.children.get(phase['epic']):
                    epics_without_task.append(phase['epic'])
            planned = {phase['anchor'] for phase in index['plan_phases']}
            for heading in index['headings']:
                keys = {token[4:].split('@', 1)[0] for token in heading['tokens'] if token.startswith('req:')}
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
                     'plan_phases_without_epic': phases_without_epic[:LIST_MAX],
                     'epics_without_task': epics_without_task[:LIST_MAX],
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
                    reviews.append({'of': match.group(1), 'commit': match.group(2), 'at': comment.get('created_at')})
            flags = ['delivered-after-publication'] if published and delivery and delivery > published else []
            lanes.append({'task': lane, 'title': label(lane_row.get('title')), 'plan_at': stamp(plan),
                          'delivered_at': stamp(delivery), 'cross_reviews': reviews[:LIST_MAX], 'flags': flags})
        design = designs.get(name)
        converged = None
        if design is not None:
            # The converged design and its decision are read from the catalog entry, never from comments.
            converged = {'key': design['key'], 'selected': design['record'] is not None,
                         'revision': design['shown'].get('revision'),
                         'decisions': list((design['shown'].get('decisions') or [])[:LIST_MAX])}
        rows.append({'epic': task, 'scope': name, 'title': label(row.get('title')),
                     'published_at': stamp(published),
                     'publication': 'recorded' if published else 'publication not recorded',
                     'lanes': lanes, 'converged': converged,
                     'flags': sorted({flag for lane in lanes for flag in lane['flags']})})
        if len(rows) >= ROUNDS_MAX:
            break
    return rows


BUILDERS = {'requirements': requirements_view, 'tasks': tasks_view, 'tokens': tokens_view,
            'decisions': decisions_view, 'designs': designs_view}


def summary_view(snap):
    """The Coverage panel's lines: counts only, from the same snapshot as every view."""
    has_designs = bool(snap.designs())
    requirements = requirements_view(snap)
    tokens = tokens_view(snap)
    lines = [{'view': 'requirements', 'total': len(requirements),
              'counts': {flag: sum(flag in row['flags'] for row in requirements)
                         for flag in ('no-task', 'no-capability') + (('no-design',) if has_designs else ())}},
             {'view': 'tokens', 'total': len(tokens), 'counts': {}}]
    if has_designs:
        tasks = tasks_view(snap)
        decisions = decisions_view(snap)
        designs = designs_view(snap)
        rounds = round_view(snap)
        lines += [{'view': 'tasks', 'total': len(tasks),
                   'counts': {flag: sum(flag in row['flags'] for row in tasks)
                              for flag in ('cites-superseded-revision', 'cites-nothing')}},
                  {'view': 'decisions', 'total': sum(row['kind'] == 'decision' for row in decisions),
                   'counts': {'possible-decision-in-comment': sum(row['kind'] != 'decision' for row in decisions)}},
                  {'view': 'designs', 'total': len(designs),
                   'counts': {flag: sum(flag in row['flags'] for row in designs)
                              for flag in ('not-selected', 'unverified', 'design-drift', 'section-without-plan-phase',
                                           'plan-phase-without-epic', 'epic-without-task')}},
                  {'view': 'round', 'total': len(rounds),
                   'counts': {'delivered-after-publication': sum(bool(row['flags']) for row in rounds)}}]
    return lines


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
        text += '; possible decisions: the newest %d of %d comments scanned' % scanned
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
    while True:
        answer['rows'] = page
        answer['next_offset'] = offset + len(page) if offset + len(page) < len(rows) else None
        if len(page) <= 1 or encoded_size(json.dumps(answer, ensure_ascii=False)) <= ANSWER_MAX:
            break
        page = page[:max(1, len(page) // 2)]
    return answer


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
            if len(value) > 73 or not re.fullmatch(r'[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*', value):
                raise ValueError(USAGE)
            options['scope'] = value
        else:
            if not re.fullmatch(r'[A-Za-z0-9:_-]{1,80}', value):
                raise ValueError(USAGE)
            options['generation'] = value
    return options


def rows_generation(rows):
    """A generation for an export with no tracker HEAD to name it: a digest of the rows themselves."""
    import hashlib
    import json
    return 'r:' + hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:32]


def answer(rows, project, options, *, operators=None, verifiers=None, members=None, journal=None, generation=None,
           now=None):
    """One page for the endpoint action: the snapshot, the stale-page check and the generation it was read at.

    The requirements-governance snapshot is read (from `journal`, the project directory) only when the
    export holds a requirement row, as `brief` reads it only for a `req:` token."""
    generation = generation or rows_generation(rows)
    if options['generation'] is not None and options['generation'] != generation:
        raise ValueError(STALE)
    governance = None
    if journal is not None and any(isinstance(row, dict) and 'requirement' in (row.get('labels') or []) for row in rows):
        import requirement_governance
        try:
            governance = requirement_governance.snapshot(journal, project)
        except (ValueError, OSError):
            governance = None
    snap = Snapshot(rows, project, now=snapshot_now(now), operators=operators, verifiers=verifiers,
                    governance=governance, members=members)
    result = trace(snap, options['view'], scope=options['scope'], offset=options['offset'], limit=options['limit'])
    result['generation'] = generation
    return result


def snapshot_now(value=None):
    """The server's clock as an aware datetime; tests pass a fixed one."""
    return value if value is not None else datetime.now(timezone.utc)
