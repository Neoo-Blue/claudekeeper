#!/usr/bin/env python3
"""
Tests for the pty supervisor. The unit tests cover the pure logic. The
integration test drives the real supervisor loop against a fake claude in a
pseudo terminal, so the detect then inject path is verified without any quota.
Run with: python3 test/watch-tests.py
"""

import os
import sys
import time
import subprocess
import tempfile
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WATCH = os.path.join(ROOT, "bin", "keeper-watch.py")
FAKE = os.path.join(HERE, "fake-claude-tui.py")

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s\n       %s" % (name, detail))


def load_watch():
    spec = importlib.util.spec_from_file_location("keeper_watch", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


w = load_watch()

# --- unit: dedupe_chain -----------------------------------------------------
check("dedupe collapses duplicate models", w.dedupe_chain(["opus", "opus"]) == ["opus"])
check("dedupe keeps order", w.dedupe_chain(["fable", "opus"]) == ["fable", "opus"])
check("dedupe is case insensitive", w.dedupe_chain(["Fable", "fable", "opus"]) == ["Fable", "opus"])

# --- unit: decide_on_limit --------------------------------------------------
check("fable limit switches to opus", w.decide_on_limit(0, 2, True, True) == ("switch", 1))
check("last model waits", w.decide_on_limit(1, 2, True, True) == ("wait", 0))
check("switch disabled waits", w.decide_on_limit(0, 2, False, True) == ("wait", 0))
check("no switch no wait stops", w.decide_on_limit(1, 2, True, False)[0] == "stop")

# --- unit: parse_reset_epoch ------------------------------------------------
check("pipe seconds", w.parse_reset_epoch("usage limit reached|1712345678") == 1712345678.0)
check("pipe millis", w.parse_reset_epoch("usage limit reached|1712345678000") == 1712345678.0)
now = 1_000_000.0
val = w.parse_reset_epoch("Your limit will reset at 3pm", now=now)
check("human 3pm is in the future", val is not None and val > now, str(val))
check("no reset returns None", w.parse_reset_epoch("usage limit reached, sorry") is None)

# --- unit: LIMIT_RE ---------------------------------------------------------
check("detects usage limit reached", w.LIMIT_RE.search("Claude AI usage limit reached") is not None)
check("does not fire on normal text", w.LIMIT_RE.search("running the tests now") is None)

# --- unit: ANSI stripping ---------------------------------------------------
stripped = w.ANSI_RE.sub(b"", b"\x1b[31musage limit reached\x1b[0m")
check("ansi strip leaves plain text", stripped == b"usage limit reached", repr(stripped))

# --- integration: pty detect then inject ------------------------------------
def integration():
    fd, log_path = tempfile.mkstemp(prefix="keeper-it-")
    os.close(fd)
    open(log_path, "w").close()
    env = dict(os.environ)
    env.update({
        "CLAUDEKEEPER_CLAUDE_BIN": FAKE,
        "FAKE_LOG": log_path,
        "CLAUDEKEEPER_FORCE_MODEL": "0",
        "CLAUDEKEEPER_IDLE_SECONDS": "0.3",
        "CLAUDEKEEPER_COOLDOWN_SECONDS": "2",
        "CLAUDEKEEPER_PRIMARY_MODEL": "fable",
        "CLAUDEKEEPER_FALLBACK_MODELS": "opus",
    })
    try:
        proc = subprocess.run(
            [sys.executable, WATCH],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        return False, "supervisor timed out: %s" % exc
    with open(log_path) as f:
        injected = f.read()
    os.unlink(log_path)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    detail = "exit=%s\ninjected=%r\nstdout=%r" % (proc.returncode, injected, out)
    ok = (
        "/model opus" in injected
        and "continue" in injected.lower()
        and "resumed work now" in out
        and proc.returncode == 0
    )
    return ok, detail


make_fake_executable = True
try:
    os.chmod(FAKE, 0o755)
except OSError:
    pass

ok, detail = integration()
check("pty supervisor switches to opus and continues", ok, detail)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
