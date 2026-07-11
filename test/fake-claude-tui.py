#!/usr/bin/env python3
"""
A stand in for the interactive claude, used only by the pty integration test.
It prints a usage limit, then models the real terminal flow: a /model command
brings up a switch confirmation that must be accepted before the switch takes,
and only then does a continue message resume the work. It records everything the
supervisor types so the test can assert the full recovery. No quota is involved.
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

expecting_confirm = False
while True:
    line = sys.stdin.readline()
    if line == "":
        break
    logline(line)
    text = line.strip()
    if expecting_confirm and text == "":
        print("switched model. resumed on the new model.", flush=True)
        expecting_confirm = False
        continue
    if not text:
        continue
    if text.startswith("/model"):
        print("Switch model? Yes, switch to Opus 4.8", flush=True)
        expecting_confirm = True
        continue
    if "continue" in text.lower():
        print("resumed work now", flush=True)
        break

sys.exit(0)
