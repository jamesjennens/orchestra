"""Failing-refresh evidence for kittrial-5bb.4 (koopa, disposable).

Bootstraps a scratch project, refreshes once, then makes bd unavailable so
refresh fails; verifies the failure is explicit and prior views unchanged.
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

BRANCH = Path("/tmp/orchestra-gatetest/kitcheck")
RT = Path("/home/james/beads-team-pilot/runtime")
sys.path.insert(0, str(BRANCH))
ACTOR = "tester/session1"
PROJECT = "disptest"

from admin import config, environment  # noqa: E402

cfg = config(RT)
path = RT / "projects" / PROJECT
subprocess.run(
    [str(RT / "bin/dolt"), "--host", "127.0.0.1", "--port", str(cfg["port"]),
     "--no-tls", "--user", "root", "sql", "-q",
     "DROP DATABASE IF EXISTS %s;" % PROJECT],
    env=environment(RT), capture_output=True, text=True, timeout=60)
shutil.rmtree(path, ignore_errors=True)
path.mkdir(parents=True, exist_ok=True)
init = subprocess.run(
    [str(RT / "bin/bd"), "init", "--sandbox", "--server", "--external",
     "--server-host", "127.0.0.1", "--server-port", str(cfg["port"]),
     "--server-user", "root", "--prefix", PROJECT, "--database", PROJECT,
     "--skip-agents", "--skip-hooks", "--non-interactive"],
    env=environment(RT), cwd=str(path), capture_output=True, text=True,
    timeout=120)
init.check_returncode()
print("scratch bootstrap ok")


def ep(req):
    import endpoint
    from admin import root_path
    try:
        return endpoint.execute(root_path(str(RT)), req)
    except ValueError as e:
        return {"returncode": 2, "stdout": "", "stderr": str(e)}
    except OSError as e:
        return {"returncode": 2, "stdout": "",
                "stderr": "%s: %s" % (type(e).__name__, e)}


def view_hash():
    r = ep({"project": PROJECT, "actor": ACTOR, "action": "view",
            "path": "issues.jsonl"})
    assert r["returncode"] == 0
    return hashlib.sha256(r["stdout"].encode("utf-8")).hexdigest()


def main():
    r = ep({"project": PROJECT, "actor": ACTOR, "action": "refresh"})
    assert r["returncode"] == 0, "need a good refresh first"
    before = view_hash()
    print("good refresh ok; views hash", before[:12])
    bd_bin = RT / "bin" / "bd"
    saved = RT / "bin" / "bd.saved"
    shutil.move(str(bd_bin), str(saved))
    try:
        r = ep({"project": PROJECT, "actor": ACTOR, "action": "refresh"})
        print("failing refresh rc:", r["returncode"], "stderr:", r["stderr"][:100])
        assert r["returncode"] != 0
    finally:
        shutil.move(str(saved), str(bd_bin))
    after = view_hash()
    print("after failed refresh views hash", after[:12])
    assert after == before
    print("FAILING-REFRESH EVIDENCE: explicit failure, prior views preserved: OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(RT / "projects" / PROJECT, ignore_errors=True)
        print("scratch project removed")
