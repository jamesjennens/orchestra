#!/usr/bin/env python3
"""Disposable canonical endpoint used by the HTTP binding tests.

It speaks the same stdin/stdout JSON envelope as ``endpoint.py`` and persists its
canonical records in ``<root>/canonical.json``. It deliberately is *not* permissive:
the canonical binding tests must fail wherever the deployed endpoint's validation
fails. All of that behaviour is shared with the round-3 probe through
``tools/strict_canonical_endpoint.py``:

* the ``endpoint.py`` transport rules (raw file flags are rejected; file bodies
  travel as ``@attachment:`` items and are materialized server-side),
* the real canonical validators from this checkout (``review_workflow.validate``,
  ``briefing.save_checkpoint``, ``briefing.history_page`` with its 1..20 limit and
  snapshot cursor),
* the shared ``http_authority`` live-authority re-validation and durable operation
  journal, so the endpoint refuses a mutation whose authority was revoked first and
  replays a committed operation instead of repeating it.

The effect is an emulated ``bd`` over the JSON row store, not a real Beads database;
that is the documented disposable-runtime boundary.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = str(ROOT / 'tools')
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from strict_canonical_endpoint import main  # noqa: E402

if __name__ == '__main__':
    main()
