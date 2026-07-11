#!/usr/bin/env python3
"""
A simulated claude session used by `claudekeeper selftest`. It uses no quota. By
default it shows a Fable credit limit so you can watch the switch to Opus. Set
SELFTEST_SCENARIO=session to watch the other path: keep the model, wait for the
session to refresh, then continue.
"""

import os
import sys
import time

scenario = os.environ.get("SELFTEST_SCENARIO", "fable")


def out(s):
    print(s, flush=True)


out("[selftest] simulated claude session. No quota is used here.")
out("[selftest] working on your task ...")
time.sleep(0.6)

if scenario == "session":
    out("[selftest] pretending the whole session hit its usage limit:")
    time.sleep(0.3)
    out("You've reached your usage limit. Resets in 3 seconds.")
    out("[selftest] keeper should keep the model and wait, not switch ...")
else:
    out("[selftest] pretending Fable just ran out of credits:")
    time.sleep(0.3)
    out("You're out of usage credits. Run /usage-credits to keep using Fable 5 or /model to switch models.")
    out("[selftest] keeper should switch to Opus and continue ...")

expecting_confirm = False
while True:
    line = sys.stdin.readline()
    if line == "":
        break
    text = line.strip()
    if expecting_confirm and text == "":
        out("[selftest] confirmed. Switched to Opus. Composer ready again.")
        expecting_confirm = False
        continue
    if not text:
        continue
    if text.startswith("/model"):
        out("[selftest] keeper typed: %s" % text)
        out("Switch model? Yes, switch to Opus 4.8   (keeper will confirm this)")
        expecting_confirm = True
        continue
    if "continue" in text.lower():
        out("[selftest] keeper sent a continue. Resumed. Success.")
        break
    out("[selftest] session received: %s" % text)

out("[selftest] done. This is exactly how a real limit would be handled.")
