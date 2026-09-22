"""Native export-completeness investigation for kittrial-5bb.4 (koopa).

Runs ON KOOPA against the REAL installed endpoint with a disposable
MySQL-backed project (removed afterwards; kittrial untouched). Produces the
evidence the coordinator requested:
- installed kit/runtime identity (paths, VERSION)
- exact raw export bytes/sha256/counts from native `bd export --all`
- controlled snapshot: 5 tasks x 20 comments incl. large bodies
- endpoint refresh -> view issues.jsonl: identity sets + byte comparison
- concurrent refresh/view at the shared endpoint (3 parallel refreshes + view)
- failing refresh: prior views preserved, failure explicit
"""
import hashlib
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

BRANCH = Path("/tmp/orchestra-gatetest/kitcheck")
KIT = Path("/home/james/beads-team-pilot/kit")
RT = Path("/home/james/beads-team-pilot/runtime")
sys.path.insert(0, str(BRANCH))

ACTOR = "tester/session1"
PROJECT = "disptest"


def bd(*args):
    from admin import environment
    import subprocess
    return subprocess.run(
        [str(RT / "bin/bd"), "--directory", str(RT / "projects" / PROJECT),
         "--sandbox", "--actor", ACTOR, *args],
        env=environment(RT), capture_output=True, text=True, timeout=180)


def ep(req):
    import endpoint
    from admin import root_path
    try:
        return endpoint.execute(root_path(str(RT)), req)
    except ValueError as e:
        return {"returncode": 2, "stdout": "", "stderr": str(e)}


def identity_sets(rows):
    issues = {r["id"] for r in rows}
    comments = {(r["id"], str(c["id"])) for r in rows for c in (r.get("comments") or [])}
    return issues, comments


def raw_export():
    p = bd("export", "--all")
    assert p.returncode == 0, p.stderr[:300]
    return p.stdout.encode("utf-8")


def main():
    from admin import config, environment
    import subprocess
    version = (KIT / "VERSION").read_text().strip()
    print("kit:", KIT, "VERSION", version)
    print("root:", RT)
    print("endpoint:", KIT / "endpoint.py")
    print("branch checkout:", BRANCH)

    cfg = config(RT)
    path = RT / "projects" / PROJECT
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(RT / "bin/bd"), "init", "--sandbox", "--server", "--external",
         "--server-host", "127.0.0.1", "--server-port", str(cfg["port"]),
         "--server-user", "root", "--prefix", PROJECT, "--database", PROJECT,
         "--skip-agents", "--skip-hooks", "--non-interactive"],
        env=environment(RT), cwd=str(path), capture_output=True,
        text=True, timeout=120, check=True)
    print("init ok")

    for i in range(5):
        task = json.loads(bd("create", "Native task %d" % i, "--json").stdout)["id"]
        for j in range(20):
            size = 50000 if j % 5 == 0 else 200
            body = "note %d on %s " % (j, task) + "x" * size
            r = bd("comments", "add", task, body, "--json")
            assert r.returncode == 0, r.stderr[:200]
    print("snapshot created: 5 tasks x 20 comments (incl. 50KB bodies)")

    # 1. Native read: exact raw export bytes/hash/counts + identity sets.
    raw = raw_export()
    native_rows = [json.loads(x) for x in raw.decode().splitlines() if x.strip()]
    n_issues, n_comments = identity_sets(native_rows)
    print("native export: bytes=%d sha256=%s issues=%d comments=%d"
          % (len(raw), hashlib.sha256(raw).hexdigest(), len(n_issues), len(n_comments)))

    # 2. Endpoint refresh, then view the export file without truncation.
    r = ep({"project": PROJECT, "actor": ACTOR, "action": "refresh"})
    assert r["returncode"] == 0, r["stderr"][:300]
    rendered = json.loads(r["stdout"])
    print("refresh summary:", rendered)
    v = ep({"project": PROJECT, "actor": ACTOR, "action": "view",
            "path": "issues.jsonl"})
    assert v["returncode"] == 0
    view_bytes = v["stdout"].encode("utf-8")
    view_rows = [json.loads(x) for x in v["stdout"].splitlines() if x.strip()]
    v_issues, v_comments = identity_sets(view_rows)
    print("view file: bytes=%d sha256=%s" % (len(view_bytes), hashlib.sha256(view_bytes).hexdigest()))
    print("identity sets equal native:", (n_issues, n_comments) == (v_issues, v_comments))
    assert (n_issues, n_comments) == (v_issues, v_comments)
    assert rendered["issues"] == len(n_issues) and rendered["comments"] == len(n_comments)
    biggest = max((c.get("text", "") for row in view_rows
                   for c in (row.get("comments") or [])), key=len)
    assert len(biggest) > 50000, len(biggest)
    print("large comment body roundtrips intact:", len(biggest), "chars")

    # 3. Concurrent refresh/view at the shared endpoint.
    results = []

    def do_refresh():
        r = ep({"project": PROJECT, "actor": ACTOR, "action": "refresh"})
        results.append(("refresh", r["returncode"], r["stderr"][:80]))

    def do_view():
        r = ep({"project": PROJECT, "actor": ACTOR, "action": "view",
                "path": "issues.jsonl"})
        results.append(("view", r["returncode"],
                        hashlib.sha256(r["stdout"].encode("utf-8")).hexdigest()[:12]))

    threads = [threading.Thread(target=do_refresh) for _ in range(3)] + \
              [threading.Thread(target=do_view)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for kind, rc, extra in results:
        print("concurrent", kind, "rc:", rc, extra if rc else "")
        assert rc == 0, (kind, rc, extra)
    final = ep({"project": PROJECT, "actor": ACTOR, "action": "view",
                "path": "issues.jsonl"})
    final_rows = [json.loads(x) for x in final["stdout"].splitlines() if x.strip()]
    assert identity_sets(final_rows) == (n_issues, n_comments)
    print("post-concurrency identity sets equal native: OK")

    # 4. Failing refresh: prior views preserved, failure explicit.
    before = ep({"project": PROJECT, "actor": ACTOR, "action": "view",
                 "path": "issues.jsonl"})
    before_hash = hashlib.sha256(before["stdout"].encode("utf-8")).hexdigest()
    bd_bin = RT / "bin" / "bd"
    saved = RT / "bin" / "bd.saved"
    shutil.move(str(bd_bin), str(saved))
    try:
        r = ep({"project": PROJECT, "actor": ACTOR, "action": "refresh"})
        print("failing refresh rc:", r["returncode"], "stderr:", r["stderr"][:80])
        assert r["returncode"] != 0
    finally:
        shutil.move(str(saved), str(bd_bin))
    after = ep({"project": PROJECT, "actor": ACTOR, "action": "view",
                "path": "issues.jsonl"})
    after_hash = hashlib.sha256(after["stdout"].encode("utf-8")).hexdigest()
    assert after_hash == before_hash
    print("failing refresh preserved prior views (hash unchanged): OK")
    print("ALL NATIVE INVESTIGATION CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(RT / "projects" / PROJECT, ignore_errors=True)
        print("scratch project removed")



