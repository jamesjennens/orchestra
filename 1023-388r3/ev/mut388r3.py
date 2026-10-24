"""Apply one named mutant to a checkout copy: mut388r3.py <checkout> <name>. Exact, once-only replacements.
M1-M16 are round 1's (M12 and M14 re-anchored for r2); M17+ are new for r2."""
import sys
from pathlib import Path

M = {
 "M1-role-recheck": ("requirement_http.py", "if editor.get('role') not in ('owner', 'coordinator'):", "if False:"),
 "M2-via-session": ("requirement_http.py", "if descriptor.get('via') != 'session':", "if False:"),
 "M3-project-match": ("requirement_http.py", "if descriptor.get('project') != project:", "if False:"),
 "M4-route-r7": ("http_service.py", "if ctx.principal.via != 'session' or ctx.principal.credential_id or ctx.principal.agent_id:", "if False:"),
 "M5-item-content-bind": ("requirement_http.py", "if (item['title'] != body['title'] or item['text'] != body['text']", "if False and (item['title'] != body['title'] or item['text'] != body['text']"),
 "M6-item-state-bind": ("requirement_http.py", "if item['moderated'] or item['item_kind'] != 'decision' or \\", "if False and \\"),
 "M7-cache-stale-check": ("rooms.py", "        if not isinstance(entry.get('items'), list):\n            stale = True\n            break\n", ""),
 "M8-cache-refresh-on-append": ("rooms.py", "    if line['kind'] in ('item', 'item-event'):\n        entry['items'] = items_from_lines(load_lines(project, room_id), room_id)\n", "    if False:\n        pass\n"),
 "M9-moderated-items-filter": ("rooms.py", "        target.update({'moderated': True, 'title': None, 'text': tombstone['text'],", "        target.update({'moderated': True,"),
 "M10-jsonl-item-tombstone": ("rooms.py", "        if line['kind'] in ('message', 'item'):\n            tombstone = tombstones.get(line['sequence'])", "        if line['kind'] in ('message',):\n            tombstone = tombstones.get(line['sequence'])"),
 "M11-endpoint-flock": ("endpoint.py", "            fcntl.flock(lock,fcntl.LOCK_EX)\n            answer=room_decision(", "            answer=room_decision("),
 "M12-route-accept-lock": ("http_service.py", "                    with self._rooms_lock(project):\n                        result = rooms.accept_item(", "                    with __import__('contextlib').nullcontext():\n                        result = rooms.accept_item("),
 "M13-service-only": ("endpoint.py", "'task-file','room-decision')\n\ndef key_project_refusal", "'task-file')\n\ndef key_project_refusal"),
 "M14-decide-moderated": ("rooms.py", "not accepted.')\n    if current['moderated']:\n        raise Refusal(409, 'moderated', 'A moderated item cannot be decided.')", "not accepted.')\n    if False:\n        pass"),
 "M15-route-decision-editor": ("http_service.py", "            raise forbidden('Only the project owner or a coordinator can decide a decision item.')", "            return CAP_COORDINATE"),
 "M16-block-capability": ("http_authority.py", "    'room-decision': {'issue': CAP_COORDINATE},", "    'room-decision': {'issue': CAP_READ},"),
 # ---- new for r2 ----
 "M14b-decidable-moderated": ("rooms.py", "No such item in this room.')\n    if current['moderated']:\n        raise Refusal(409, 'moderated', 'A moderated item cannot be decided.')", "No such item in this room.')\n    if False:\n        pass"),
 "M17r3-no-item-lock": ("http_service.py", "refusing to use it.')\n        return file_lock(lock)", "refusing to use it.')\n        return __import__('contextlib').nullcontext()"),
 "M18-no-existing-refusal": ("http_service.py", "                    if existing is not None:\n                        # An exact retry", "                    if False:\n                        # An exact retry"),
 "M19-no-rights": ("http_service.py", "        if item.get('author', {}).get('id') != ctx.principal.user_id:", "        if False:"),
 "M20-no-decidable": ("http_service.py", "current = rooms.decidable_item(project, ctx.params['rid'],\n                                                   current['sequence'], 'accept')", "current = current"),
 "M22-no-endpoint-room-open": ("requirement_http.py", "        if room['state'] != 'open':\n            raise ValueError('This room is archived", "        if False:\n            raise ValueError('This room is archived"),
 "M23-no-event-archived-project": ("http_service.py", "        self._rooms_item_session(ctx)\n        record, role = self._project(ctx, CAP_READ)\n        if isinstance(record, dict) and record.get('archived'):", "        self._rooms_item_session(ctx)\n        record, role = self._project(ctx, CAP_READ)\n        if False:"),
 "M24-no-amend-title-redaction": ("rooms.py", "        if 'title' in view:\n            view['title'] = None", "        if False:\n            view['title'] = None"),
 "M25-no-live-archive-recheck": ("http_service.py", "if isinstance(live, dict) and live.get('archived'):", "if False:"),
 "M26-no-post-credential-refusal": ("http_service.py", "        if ctx.principal.via == 'credential':\n            raise forbidden('Rooms are for people in this slice: an agent credential cannot write to a room.')\n        record, role = self._project(ctx, CAP_ROOMS_POST)", "        record, role = self._project(ctx, CAP_ROOMS_POST)"),
 "M27-no-rights-recheck-in-lock": ("http_service.py", "                    self._rooms_item_rights(ctx, current, role, event)\n", ""),
 # ---- new for r3 ----
 "M21r3-no-parent-job-check": ("http_service.py", "                        if chosen not in ids:", "                        if False:"),
 "M28-amend-stable-identity": ("http_service.py", "content=rooms.item_content_digest(title, text, parent))", "content=None)"),
 "M29-accept-stable-identity": ("http_service.py", "content=rooms.item_content_digest(current['title'], current['text']))", "content=None)"),
 "M30-archive-no-item-lock": ("http_service.py", "reason='room %s' % ctx.params['rid'], item_first=True)", "reason='room %s' % ctx.params['rid'], item_first=False)"),
 "M31-item-lock-follows-symlink": ("http_service.py", "        if lock.is_symlink():\n            raise HttpError(500, 'damaged',\n", "        if False:\n            raise HttpError(500, 'damaged',\n"),
 "M32-room-lock-follows-symlink": ("http_service.py", "    if lock.is_symlink():\n        raise HttpError(500, 'damaged', 'The rooms lock", "    if False:\n        raise HttpError(500, 'damaged', 'The rooms lock"),
 "M33-amend-parent-unchecked": ("http_service.py", "if parent not in [job.get('id') for job in jobs]:", "if False:"),
 "M34-editor-choice-ignored": ("http_service.py", "chosen = (payload_parent or current.get('parent')) if editor", "chosen = (current.get('parent') or payload_parent) if editor"),
 "M35-moderated-item-parent-served": ("rooms.py", "                       'parent': None})", "                       })"),
 "M36-moderated-amend-parent-served": ("rooms.py", "        if 'parent' in view:\n            view['parent'] = None", "        if False:\n            view['parent'] = None"),
 "M37-empty-jobs-skip": ("http_service.py", "                        if chosen not in ids:", "                        if ids and chosen not in ids:"),
 "M38-replay-ignores-parent": ("rooms.py", "and existing.get('parent') == parent)", ")"),
}
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
