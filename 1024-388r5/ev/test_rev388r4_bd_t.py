"""Reviewer: round-4 real-bd repros with a longer HTTP client timeout (setUp timed out under load)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_rev388r4_bd as r4


class Rev388r4BdT(r4.Rev388r4Bd):
    http_timeout = 180
