"""Run the portable suite and surface individual failures as CI annotations."""
import os
import sys
import unittest
from pathlib import Path

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
suite=unittest.defaultTestLoader.discover(str(root/'tests'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
if os.environ.get('GITHUB_ACTIONS')=='true':
    for case,trace in result.failures+result.errors:
        message=(str(case)+'\n'+trace).replace('%','%25').replace('\r','%0D').replace('\n','%0A')
        print('::error::'+message,flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
