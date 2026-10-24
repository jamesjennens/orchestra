import sys
from pathlib import Path
p = Path(sys.argv[1])
lines = p.read_text(encoding='utf-8').splitlines(keepends=True)
out = []
for line in lines:
    if line.lstrip().startswith('"M14b-decidable-moderated"'):
        line = (' "M14br4-decidable-moderated": ("rooms.py", '
                '"    if current[\'moderated\']:\\n        raise Refusal(409, \'moderated\', \'A moderated item cannot be decided.\')\\n'
                '    if current[\'state\'] not in (\'proposed\', \'amended\'):\\n        raise Refusal(409, \'final\', \'That item is already decided.\')\\n'
                '    if event == \'accept\' and current[\'item_kind\'] == \'question\':", '
                '"    if current[\'state\'] not in (\'proposed\', \'amended\'):\\n        raise Refusal(409, \'final\', \'That item is already decided.\')\\n'
                '    if event == \'accept\' and current[\'item_kind\'] == \'question\':"),\n')
    out.append(line)
p.write_text(''.join(out), encoding='utf-8')
print('ok')
