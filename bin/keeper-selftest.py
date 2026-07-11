#!/usr/bin/env python3
"""
A simulated claude session used by `claudekeeper selftest`. It uses no quota. It
prints a run, then a realistic usage limit, then models the real switch flow: a
/model command brings up a confirmation that must be accepted, and only then
does the continue resume the work. Watch it happen live in your own terminal.
"""

import os
import sys
import time


def out(s):
    print(s, flush=True)


out("[selftest] simulated claude session. No quota is used here.")
out("[selftest] working on your task ...")
time.sleep(0.6)
out("[selftest] the next line pretends the model just ran out of Fable budget:")
time.sleep(0.3)
out("Claude usage limit reached. Your Fable limit resets at 3pm.")
out("[selftest] now waiting to see what claudekeeper does ...")

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
        out("[selftest] keeper sent a continue. Resumed on Opus. Success.")
        break
    out("[selftest] session received: %s" % text)

out("[selftest] done. This is exactly how a real limit would be handled.")
