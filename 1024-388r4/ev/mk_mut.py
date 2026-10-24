"""Build ev/mut388r4.py from round 3's mut388r3.py: re-anchor M28, add r4 mutants M39-M47."""
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text(encoding='utf-8')
old28 = '''"M28-amend-stable-identity": ("http_service.py", "content=rooms.item_content_digest(title, text, parent))", "content=None)"),'''
new28 = '''"M28r4-amend-stable-identity": ("http_service.py", "content=rooms.item_content_digest(title, text, parent,\\n                                                              'after-%d' % previous))", "content=None)"),'''
assert src.count(old28) == 1
src = src.replace(old28, new28)
add = r'''
 # ---- new for r4 ----
 "M39-no-intent": ("http_service.py", "rooms.intend_accept(project, ctx.params['rid'], current['sequence'],\n                                            operation_id, actor, limits_values)", "pass"),
 "M40-decide-ignores-pending": ("rooms.py", "if pending is not None and not (event == 'accept' and operation_id == pending['identity']):", "if False:"),
 "M41-moderate-ignores-pending": ("rooms.py", "if target['kind'] == 'item' and pending_accept_intent(lines, target_sequence) is not None:", "if False:"),
 "M42-supersede-ignores-pending": ("rooms.py", "if pending_accept_intent(lines, supersedes) is not None:", "if False:"),
 "M43-amend-no-previous": ("http_service.py", "parent,\n                                                              'after-%d' % previous))", "parent))"),
 "M44-jsonl-parent-kept": ("rooms.py", "            safe.pop('parent', None)\n", ""),
 "M45-amend-noop-replay-removed": ("rooms.py", "        if current['title'] == title and current['text'] == text and \\\n                (parent is None or parent == current.get('parent')):\n            return dict(current, replayed=True)\n", ""),
 "M46-intent-any-identity": ("rooms.py", "        if pending['identity'] == identity:\n            return dict(pending, replayed=True)", "        if True:\n            return dict(pending, replayed=True)"),
 "M47-read-serves-intent": ("rooms.py", "        if line['kind'] in ('tombstone', 'item-intent'):\n            continue", "        if line['kind'] in ('tombstone',):\n            continue"),
}
root, name = Path(sys.argv[1]), sys.argv[2]'''
anchor = '\n}\nroot, name = Path(sys.argv[1]), sys.argv[2]'
assert src.count(anchor) == 1
src = src.replace(anchor, add)
src = src.replace('mut388r3.py', 'mut388r4.py')
Path(sys.argv[2]).write_text(src, encoding='utf-8')
print('ok')
