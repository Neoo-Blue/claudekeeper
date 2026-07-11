#!/usr/bin/env python3
"""
A simulated claude session used by `claudekeeper selftest`. It uses no quota. It
prints a short run, then a realistic usage limit line, then shows exactly what
the supervisor types back into the session, so you can watch the switch to the
fallback model and the continue happen live in your own terminal.
"""

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

while True:
    line = sys.stdin.readline()
    if line == "":
        break
    text = line.strip()
    if not text:
        continue
    if text.startswith("/model"):
        out("[selftest] keeper sent a model command: %s" % text)
    elif "continue" in text.lower():
        out("[selftest] keeper sent a continue. Resumed on the new model. Success.")
        break
    else:
        out("[selftest] session received: %s" % text)

out("[selftest] done. This is exactly how a real limit would be handled.")
