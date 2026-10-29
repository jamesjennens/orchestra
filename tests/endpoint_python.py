#!/usr/bin/env python3
import os
import sys
from pathlib import Path

environment = dict(os.environ)
shim = Path(environment.get('HOME', '')) / 'future_clock.so'
if shim.is_file():
    environment['LD_PRELOAD'] = str(shim)
os.execvpe(sys.executable, [sys.executable, *sys.argv[1:]], environment)
