import json, sys, os
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import rooms
base = Path("/dev/shm/orc-review-388r1/segproj"); base.mkdir(parents=True, exist_ok=True)
actor = {"id": "usr_a", "name": "a", "role": "owner"}
L = dict(rooms.LIMIT_DEFAULTS)
def fresh(tag):
    p = base / tag; p.mkdir()
    r = rooms.create(p, "R", actor, L)["id"]
    rooms.post_item(p, r, "requirement", "T", "X", "op-item-00001", actor, L)
    rec = {"kind":"proposal","id":"p-1","revision":1,"sha256":None,"decision_id":None,"evidence":"orchestra:room/%s#1"%r}
    rooms.accept_item(p, r, 1, "op-accept-0001", actor, L, rec)
    seg = sorted((p/rooms.FOLDER/r).glob("*.jsonl"))
    return p, r, seg
def show(tag, p, r):
    out = {}
    for name, fn in (("read_item", lambda: rooms.read_item(p, r, 1)["state"]),
                     ("rebuild", lambda: [i["state"] for i in rooms.rebuild_index(p)["rooms"][r]["items"]]),
                     ("read_messages.items", lambda: [i["state"] for i in rooms.read_messages(p, r)["items"]])):
        try: out[name] = fn()
        except Exception as e: out[name] = "REFUSED %s: %s" % (type(e).__name__, str(e)[:90])
    print(tag, json.dumps(out))
p, r, seg = fresh("dup"); print("segments", [s.name for s in seg])
raw = seg[0].read_bytes().splitlines(keepends=True)
item_line = [l for l in raw if b"\"kind\":\"item\"" in l or b"\"kind\": \"item\"" in l][0]
seg[0].write_bytes(b"".join(raw) + item_line); show("duplicated-item-line", p, r)
p, r, seg = fresh("mal"); seg[0].write_bytes(seg[0].read_bytes() + b"{not json}\n"); show("malformed-line", p, r)
p, r, seg = fresh("edit"); b = seg[0].read_bytes().replace(b"\"state\":\"accepted\"", b"\"state\":\"rejected\""); seg[0].write_bytes(b); show("event-state-edited", p, r)
p, r, seg = fresh("idx"); ix = p/rooms.FOLDER/rooms.INDEX; ix.write_text("{\"rooms\":", encoding="utf-8"); show("truncated-index", p, r)
