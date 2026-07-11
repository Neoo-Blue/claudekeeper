#!/usr/bin/env python3
"""
A stand in for the interactive claude, used only by the pty integration test.
It prints a line, then a usage limit line, then reads whatever the supervisor
types into the pty and records it. When it receives the continue message it
prints a resume line and exits, so the test can assert the recovery happened.
No quota is involved.
"""

import os
import sys
import time

log_path = os.environ.get("FAKE_LOG", "/dev/null")


def logline(s):
    with open(log_path, "a") as f:
        f.write(s)
        f.flush()


print("working on the task", flush=True)
time.sleep(0.2)
print("Claude usage limit reached. Your limit will reset at 3pm.", flush=True)

while True:
    line = sys.stdin.readline()
    if line == "":
        break
    logline(line)
    if "continue" in line.lower():
        print("resumed work now", flush=True)
        break

sys.exit(0)
