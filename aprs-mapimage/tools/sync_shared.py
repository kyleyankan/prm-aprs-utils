#!/usr/bin/env python3
"""Keeps the two copies of the shared modules the same.

aprs_eink.py (the picture renderer) and aprs_listen.py (the APRS-IS listener) are used by
both projects.  The master copies are in aprs-mapimage/app/; aprs-incident-feeder/ holds byte-identical
copies, because the Pi is set up by copying that one folder by hand and the Docker image
is built from aprs-mapimage/ alone.

  python3 aprs-mapimage/tools/sync_shared.py           # copy the masters into aprs-incident-feeder/
  python3 aprs-mapimage/tools/sync_shared.py --check   # change nothing; exit 1 if they differ

Edit the master, run this, and commit both copies together.
"""
import os
import sys

SHARED = ("aprs_eink.py", "aprs_listen.py")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MASTER = os.path.join(ROOT, "aprs-mapimage", "app")
COPY = os.path.join(ROOT, "aprs-incident-feeder")


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def differing():
    """Names of the shared files whose aprs-incident-feeder copy is missing or not identical to the master."""
    return [name for name in SHARED if read(os.path.join(COPY, name)) != read(os.path.join(MASTER, name))]


def sync():
    """Copy each master over a differing copy; returns the names that were written."""
    changed = differing()
    for name in changed:
        data = read(os.path.join(MASTER, name))
        if data is None:
            raise SystemExit(f"master copy missing: {os.path.join(MASTER, name)}")
        tmp = os.path.join(COPY, name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(COPY, name))
    return changed


def main(argv):
    if argv not in ([], ["--check"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if argv:
        diff = differing()
        for name in diff:
            print(f"aprs-incident-feeder/{name} is not the same as aprs-mapimage/app/{name}")
        if diff:
            print("Edit the master in aprs-mapimage/app/, then run: python3 aprs-mapimage/tools/sync_shared.py")
        return 1 if diff else 0
    changed = sync()
    for name in changed:
        print(f"copied aprs-mapimage/app/{name} -> aprs-incident-feeder/{name}")
    if not changed:
        print("aprs-incident-feeder copies already match the masters")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
