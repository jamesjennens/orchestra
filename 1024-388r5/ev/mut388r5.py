"""Apply one named mutant to a checkout copy: mut388r5.py <checkout> <name>. Exact, once-only replacements.
The round-4 reviewer's 48 (round-4 file mut388r4.py), with M18 M20 M23 M29 M39 M41 M44 M47 re-anchored
for r5 (suffix r5), plus new reviewer mutants V1-V19 for the round-5 code."""
import sys
from pathlib import Path

sys.path.insert(0, '/home/james/orchestra-review-evidence/1024-388r4/rev/ev')
import importlib.util
spec = importlib.util.spec_from_file_location('m4', '/home/james/orchestra-review-evidence/1024-388r4/rev/ev/mut388r4.py')
# mut388r4.py runs on import; read its table without executing the tail.
src = Path(spec.origin).read_text(encoding='utf-8')
table = src[src.index('M = {'):src.index('root, name = ')]
ns = {}
exec(table, ns)
M = dict(ns['M'])
for gone in ('M18-no-existing-refusal', 'M20-no-decidable', 'M23-no-event-archived-project',
             'M29-accept-stable-identity', 'M39-no-intent', 'M41-moderate-ignores-pending',
             'M44-jsonl-parent-kept', 'M47-read-serves-intent'):
    del M[gone]
M.update({
 "M18r5-no-decided-refusal": ("http_service.py", "                    if decided is not None:\n", "                    if False:\n"),
 "M20r5-no-decidable": ("http_service.py", "current = rooms.decidable_item(project, rid, current['sequence'], 'accept')", "current = current"),
 "M23r5-no-event-archived-project": ("http_service.py", "    def _rooms_item_event(self, ctx, event):\n        project = self._rooms_project(ctx)\n        self._rooms_item_session(ctx)\n        record, role = self._project(ctx, CAP_READ)\n        if isinstance(record, dict) and record.get('archived'):", "    def _rooms_item_event(self, ctx, event):\n        project = self._rooms_project(ctx)\n        self._rooms_item_session(ctx)\n        record, role = self._project(ctx, CAP_READ)\n        if False:"),
 "M29r5-accept-identity-without-content": ("rooms.py", "    content = item_content_digest(item['title'], item['text'])\n    if cancelled:", "    content = '0' * 16\n    if cancelled:"),
 "M39r5-no-intent-line": ("rooms.py", "    _append(project, room_id, line, limits_values)\n    return dict(line, replayed=False)\n\n\ndef record_attempt", "    return dict(line, replayed=False)\n\n\ndef record_attempt"),
 "M41r5-moderate-does-not-cancel": ("rooms.py", "pending = pending_accept_intent(lines, target_sequence) if target['kind'] == 'item' else None", "pending = None"),
 "M44r5-jsonl-amend-parent-kept": ("rooms.py", "            safe.pop('title', None)\n            safe.pop('parent', None)\n", "            safe.pop('title', None)\n"),
 "M47r5-read-serves-intent-lines": ("rooms.py", "        if line['kind'] == 'tombstone' or line['kind'] in INTENT_KINDS:\n            continue", "        if line['kind'] == 'tombstone':\n            continue"),
 # ---- reviewer mutants for the round-5 code ----
 "V1-jsonl-intent-parent-kept": ("rooms.py", "            safe = dict(line)\n            safe.pop('parent', None)\n", "            safe = dict(line)\n"),
 "V2-missing-row-is-none": ("http_service.py", "        entry = OperationJournal(path).lookup(operation_id)\n", "        entry = OperationJournal(path).lookup(operation_id)\n        if entry is None:\n            return ('none', None)\n"),
 "V3-uncertain-earlier-ignored": ("http_service.py", "            if state != 'none':\n                raise conflict('An earlier acceptance", "            if False:\n                raise conflict('An earlier acceptance"),
 "V4-retry-kind-from-caller": ("http_service.py", "record_kind = pending.get('record_kind') or (", "record_kind = None or ("),
 "V5-demoted-not-refused": ("http_service.py", "                    if record_kind in ('requirement', 'decision'):\n                        if not editor:", "                    if False:\n                        if not editor:"),
 "V6-proposal-author-unchecked": ("http_service.py", "                    elif intent['author']['id'] != ctx.principal.user_id:", "                    elif False:"),
 "V7-cancel-anyone": ("http_service.py", "                    if role not in ('owner', 'coordinator', 'superuser'):", "                    if False:"),
 "V8-cancel-author-any-kind": ("http_service.py", "                        if not (pending.get('record_kind') == 'proposal'\n", "                        if not (True\n"),
 "V9-archive-does-not-cancel": ("rooms.py", "        pending = pending_accept_intent(lines, item['sequence'])\n        if pending is None:\n            continue", "        pending = None\n        if pending is None:\n            continue"),
 "V10-intend-accepts-cancelled": ("rooms.py", "    if identity in _cancelled_identities(lines):\n        raise Refusal(409, 'cancelled'", "    if False:\n        raise Refusal(409, 'cancelled'"),
 "V11-identity-ignores-cancels": ("rooms.py", "    if cancelled:\n        content = '%s-c%d' % (content, cancelled)", "    if False:\n        content = '%s-c%d' % (content, cancelled)"),
 "V12-http-reserved-check-off": ("http_service.py", "if self.command == 'POST' and isinstance(key, str) and key.startswith(rooms.RESERVED_KEY_PREFIXES):", "if False:"),
 "V13-module-reserved-check-off": ("rooms.py", "    if value.startswith(RESERVED_KEY_PREFIXES):", "    if False:"),
 "V14-no-attempt-log": ("http_service.py", "        if log_attempt is not None:\n            log_attempt()", "        if False:\n            log_attempt()"),
 "V15-retry-parent-unchecked": ("http_service.py", "if payload_parent is not None and payload_parent != chosen:", "if False:"),
 "V16-no-attempt-cap": ("rooms.py", "    if len(authors) >= MAX_ATTEMPT_AUTHORS:", "    if False:"),
 "V17-unknown-not-marked": ("http_service.py", "            elif state != 'none':\n                unknown = True", "            elif False:\n                unknown = True"),
 "V18-adopt-skips-accepted-check": ("http_service.py", "                if acceptance.get('decision'):\n", "                if False:\n"),
 "V19-accept-item-accepts-cancelled": ("rooms.py", "    if event == 'accept' and operation_id in _cancelled_identities(lines):", "    if False:"),
})
root, name = Path(sys.argv[1]), sys.argv[2]
if name == "list":
    print(" ".join(M)); sys.exit(0)
path, old, new = M[name]
f = root / path
s = f.read_text(encoding="utf-8")
n = s.count(old)
if n != 1:
    sys.exit("mutant %s: %d matches" % (name, n))
f.write_text(s.replace(old, new), encoding="utf-8")
print("applied", name)
