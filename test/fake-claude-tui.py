#!/usr/bin/env python3
"""
A stand in for the interactive claude, used only by the pty integration test.
It emits one of the two real limit banners and models the matching flow, then
records everything the supervisor types so the test can assert the recovery.
No quota is involved.

FAKE_SCENARIO=fable    print a Fable credit limit; expect a /model switch that
                       must be confirmed, then a continue.
FAKE_SCENARIO=session  print a session usage limit with a short reset; expect
                       only a continue after the wait, and no /model switch.
"""

import os
import sys
import time

log_path = os.environ.get("FAKE_LOG", "/dev/null")
scenario = os.environ.get("FAKE_SCENARIO", "fable")


def logline(s):
    with open(log_path, "a") as f:
        f.write(s)
        f.flush()


print("working on the task", flush=True)
time.sleep(0.2)

if scenario == "session":
    print("You've reached your usage limit. Resets in 2 seconds.", flush=True)
else:
    print("You're out of usage credits. Run /usage-credits to keep using Fable 5 or /model to switch models.", flush=True)

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
