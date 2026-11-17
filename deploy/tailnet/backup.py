#!/usr/bin/env python3
"""A consistent copy of the tracker's database on the server (sqlite3's online backup, safe while it is in use),
into <data>/backups/, newest 30 kept. Run daily by cron on the server (deploy/tailnet/server.sh sets that up) and
by `server.sh backup` / `home`."""
import glob
import os
import sqlite3
import time

DATA = os.environ.get("TASK_TRACKER_DATA") or "/data"
OUT = os.path.join(DATA, "backups")
os.makedirs(OUT, exist_ok=True)
dest = os.path.join(OUT, "tracker-%s.sqlite3" % time.strftime("%Y%m%d-%H%M%S"))
src, dst = sqlite3.connect(os.path.join(DATA, "tracker.sqlite3")), sqlite3.connect(dest)
with dst:
    src.backup(dst)
src.close(); dst.close()
for old in sorted(glob.glob(os.path.join(OUT, "tracker-*.sqlite3")), key=os.path.getmtime, reverse=True)[30:]:
    os.remove(old)
print(time.strftime("%Y-%m-%d %H:%M:%S"), "backed up ->", dest)
