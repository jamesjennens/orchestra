"""Real service launch process groups, with disposable delayed native writers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import endpoint_process


@unittest.skipUnless(os.name == 'posix', 'Production endpoint process groups require POSIX')
class EndpointProcessTests(unittest.TestCase):
    def writer(self, directory, parent_wait):
        marker=Path(directory)/'late-state'
        child="import time;from pathlib import Path;time.sleep(1);Path(%r).write_text('written')" % str(marker)
        parent="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',%r],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(%r)" % (child,parent_wait)
        return marker,[sys.executable,'-c',parent]

    def test_endpoint_timeout_stops_delayed_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            marker,argv=self.writer(directory,10)
            with self.assertRaises(subprocess.TimeoutExpired):endpoint_process.run(argv,input='',timeout=.3)
            time.sleep(1.2)
            self.assertFalse(marker.exists(),'A timed-out endpoint left a live native writer')

    def test_normal_endpoint_return_stops_remaining_delayed_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            marker,argv=self.writer(directory,.2)
            result=endpoint_process.run(argv,input='',timeout=3)
            self.assertEqual(result.returncode,0)
            time.sleep(1.2)
            self.assertFalse(marker.exists(),'A returned endpoint left a live native writer')
