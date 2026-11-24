"""The aprs-pi copies of the shared modules must be identical to the masters in aprs-mapimage/app/.

  python3 -m unittest test_shared_copies       (from this folder; reads files only)
"""
import os
import subprocess
import sys
import unittest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "sync_shared.py")


class SharedCopies(unittest.TestCase):
    def test_copies_match_masters(self):
        r = subprocess.run([sys.executable, TOOL, "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
