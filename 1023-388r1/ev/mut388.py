"""Apply one named mutant to a checkout copy: mut388.py <checkout> <name>. Exact, once-only replacements."""
import sys
from pathlib import Path
M = {
 "M1-role-recheck": ("requirement_http.py", "if editor.get(\x27role\x27) not in (\x27owner\x27, \x27coordinator\x27):", "if False:"),
 "M2-via-session": ("requirement_http.py", "if descriptor.get(\x27via\x27) != \x27session\x27:", "if False:"),
 "M3-project-match": ("requirement_http.py", "if descriptor.get(\x27project\x27) != project:", "if False:"),
 "M4-route-r7": ("http_service.py", "if ctx.principal.via != \x27session\x27 or ctx.principal.credential_id or ctx.principal.agent_id:", "if False:"),
 "M5-item-content-bind": ("requirement_http.py", "if (item[\x27title\x27] != body[\x27title\x27] or item[\x27text\x27] != body[\x27text\x27]", "if False and (item[\x27title\x27] != body[\x27title\x27] or item[\x27text\x27] != body[\x27text\x27]"),
 "M6-item-state-bind": ("requirement_http.py", "if item[\x27moderated\x27] or item[\x27item_kind\x27] != \x27decision\x27 or \\", "if False and \\"),
 "M7-cache-stale-check": ("rooms.py", "        if not isinstance(entry.get(\x27items\x27), list):\n            stale = True\n            break\n", ""),
 "M8-cache-refresh-on-append": ("rooms.py", "    if line[\x27kind\x27] in (\x27item\x27, \x27item-event\x27):\n        entry[\x27items\x27] = items_from_lines(load_lines(project, room_id), room_id)\n", "    if False:\n        pass\n"),
 "M9-moderated-items-filter": ("rooms.py", "        target.update({\x27moderated\x27: True, \x27title\x27: None, \x27text\x27: tombstone[\x27text\x27],", "        target.update({\x27moderated\x27: True,"),
 "M10-jsonl-item-tombstone": ("rooms.py", "        if line[\x27kind\x27] in (\x27message\x27, \x27item\x27):\n            tombstone = tombstones.get(line[\x27sequence\x27])", "        if line[\x27kind\x27] in (\x27message\x27,):\n            tombstone = tombstones.get(line[\x27sequence\x27])"),
 "M11-endpoint-flock": ("endpoint.py", "            fcntl.flock(lock,fcntl.LOCK_EX)\n            answer=room_decision(", "            answer=room_decision("),
 "M12-route-accept-lock": ("http_service.py", "                with self._rooms_lock(project):\n                    result = rooms.accept_item(", "                with __import__(\x27contextlib\x27).nullcontext():\n                    result = rooms.accept_item("),
 "M13-service-only": ("endpoint.py", "\x27task-file\x27,\x27room-decision\x27)\n\ndef key_project_refusal", "\x27task-file\x27)\n\ndef key_project_refusal"),
 "M14-decide-moderated": ("rooms.py", "    if current[\x27moderated\x27]:\n        raise Refusal(409, \x27moderated\x27, \x27A moderated item cannot be decided.\x27)", "    if False:\n        pass"),
 "M15-route-decision-editor": ("http_service.py", "            raise forbidden(\x27Only the project owner or a coordinator can decide a decision item.\x27)", "            return CAP_COORDINATE"),
 "M16-block-capability": ("http_authority.py", "    \x27room-decision\x27: {\x27issue\x27: CAP_COORDINATE},", "    \x27room-decision\x27: {\x27issue\x27: CAP_READ},"),
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
