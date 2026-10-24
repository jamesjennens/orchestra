"""Reviewer runner: every test HTTP server binds 127.0.0.1 on a port in 29800-29899 (brief)."""
import sys, unittest
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
import http_service
_orig = http_service.create_server
def ranged(service, backend, host="127.0.0.1", port=0, **kw):
    last = None
    for candidate in range(29800, 29900):
        try:
            return _orig(service, backend, host="127.0.0.1", port=candidate, **kw)
        except OSError as error:
            last = error
    raise RuntimeError("no free port in 29800-29899: %s" % last)
http_service.create_server = ranged
unittest.main(module=None, argv=["revrun"] + sys.argv[1:])
