#!/usr/bin/env python3
"""
keeper-watch: transparent supervision of a live Claude Code session.

It launches the real claude inside a pseudo terminal and forwards the terminal
through unchanged, so the session looks and feels exactly like running claude
directly. In the background it watches the session output for a usage limit.
When one appears it switches to the next model in the chain (for example Fable
to the latest Opus) by driving the /model command, confirms the switch, and
sends a continue. When every model is limited it waits for the reset window and
then continues.

The real terminal UI positions text with cursor moves instead of spaces, so the
plain text scan can see words run together. The patterns below allow optional
whitespace between words for that reason.

No third party dependencies: only the Python standard library.
"""

import os
import sys
import re
import time
import signal
import select
import fcntl
import termios
import tty
import pty


# ---------------------------------------------------------------------------
# Configuration, read from the environment so the launcher can pass it through.
# ---------------------------------------------------------------------------

def _flag(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v not in ("0", "false", "no", "off")


def _num(name, default):
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


PRIMARY = os.environ.get("CLAUDEKEEPER_PRIMARY_MODEL", "fable")
FALLBACKS = [m.strip() for m in os.environ.get("CLAUDEKEEPER_FALLBACK_MODELS", "opus").split(",") if m.strip()]
SWITCH_ENABLED = _flag("CLAUDEKEEPER_SWITCH", True)
WAIT_ENABLED = _flag("CLAUDEKEEPER_WAIT", True)
FORCE_MODEL = _flag("CLAUDEKEEPER_FORCE_MODEL", True)
DRYRUN = _flag("CLAUDEKEEPER_WATCH_DRYRUN", False)
CONTINUE_MSG = os.environ.get("CLAUDEKEEPER_CONTINUE_PROMPT", "Continue exactly where you left off.")
IDLE_SECONDS = _num("CLAUDEKEEPER_IDLE_SECONDS", 2.5)
CONFIRM_TIMEOUT = _num("CLAUDEKEEPER_CONFIRM_TIMEOUT", 5.0)
MODEL_SUBMIT_DELAY = _num("CLAUDEKEEPER_MODEL_SUBMIT_DELAY", 0.7)
AFTER_CONFIRM_IDLE = _num("CLAUDEKEEPER_AFTER_CONFIRM_IDLE", 1.2)
RETRY_MINUTES = _num("CLAUDEKEEPER_RETRY_MINUTES", 20)
RESET_BUFFER_SECONDS = _num("CLAUDEKEEPER_RESET_BUFFER_SECONDS", 20)
COOLDOWN_SECONDS = _num("CLAUDEKEEPER_COOLDOWN_SECONDS", 25)
WAIT_CAP_MINUTES = _num("CLAUDEKEEPER_WAIT_CAP_MINUTES", 360)
CLAUDE_BIN = os.environ.get("CLAUDEKEEPER_CLAUDE_BIN", "claude")
LOG_PATH = os.environ.get(
    "CLAUDEKEEPER_WATCH_LOG",
    os.path.join(os.path.expanduser("~"), ".claude", "claudekeeper-watch.log"),
)

# The TUI can render words with no separating space, so allow optional space.
DEFAULT_LIMIT_PATTERN = (
    r"usage\s*limit\s*reached"
    r"|you\W{0,3}ve\s*hit\s*your\s.{0,40}limit"
    r"|(?:5|five)\W?hour\s*limit\s*reached"
    r"|weekly\s*limit\s*reached"
    r"|reached\s*your\s.{0,30}usage\s*limit"
)
LIMIT_RE = re.compile(os.environ.get("CLAUDEKEEPER_LIMIT_PATTERN", DEFAULT_LIMIT_PATTERN), re.I)

# The confirmation shown after /model, for example "Switch model? Yes, switch".
DEFAULT_DIALOG_PATTERN = r"switch\s*model|yes,?\s*switch|switch\s*to\s*(?:opus|sonnet|fable|haiku)"
DIALOG_RE = re.compile(os.environ.get("CLAUDEKEEPER_DIALOG_PATTERN", DEFAULT_DIALOG_PATTERN), re.I)

# Strip terminal control sequences so the text scan sees words, not escapes.
ANSI_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]")


def dedupe_chain(models):
    seen = set()
    out = []
    for m in models:
        if not m:
            continue
        key = m.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


CHAIN = dedupe_chain([PRIMARY] + FALLBACKS)


# ---------------------------------------------------------------------------
# Reset time parsing. Mirrors the logic in the Node CLI.
# ---------------------------------------------------------------------------

def parse_reset_epoch(text, now=None):
    if now is None:
        now = time.time()
    if not text:
        return None

    m = re.search(r"usage limit reached\s*\|\s*(\d{9,13})", text, re.I)
    if m:
        return _norm_epoch(m.group(1))

    m = re.search(r'reset(?:sAt|_at|Time)"?\s*[:=]\s*"?(\d{9,13})', text, re.I)
    if m:
        return _norm_epoch(m.group(1))

    m = re.search(r'reset(?:sAt|_at|Time)"?\s*[:=]\s*"([0-9T:\-+.Z ]{10,40})"', text, re.I)
    if m:
        try:
            import datetime
            return datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
        except (ValueError, ImportError):
            pass

    m = re.search(r"reset(?:s)?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3).lower() if m.group(3) else None
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        import datetime
        base = datetime.datetime.fromtimestamp(now)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target = target + datetime.timedelta(days=1)
        return target.timestamp()

    return None


def _norm_epoch(digits):
    n = int(digits)
    return n / 1000.0 if len(str(digits)) >= 13 else float(n)


# ---------------------------------------------------------------------------
# Decision for what to do when a limit is seen. Pure, so it is easy to test.
# ---------------------------------------------------------------------------

def decide_on_limit(chain_index, chain_length, switch_enabled, wait_enabled):
    if switch_enabled and chain_index < chain_length - 1:
        return ("switch", chain_index + 1)
    if wait_enabled:
        return ("wait", 0)
    return ("stop", chain_index)


# ---------------------------------------------------------------------------
# Building the recovery step sequence.
#
# Each step is a dict with exactly one trigger and one payload to send:
#   {"await_idle": secs, "send": b"..."}  send once output is quiet for secs
#   {"delay": secs, "send": b"..."}       send secs after the step began
#   {"await": regex, "timeout": secs, "send": b"..."}  send when regex matches
#                                                        the fresh output, or on
#                                                        timeout as a fallback
# ---------------------------------------------------------------------------

def build_switch_sequence(model, continue_after=True):
    steps = [
        {"await_idle": IDLE_SECONDS, "send": ("/model %s" % model).encode()},
        {"delay": MODEL_SUBMIT_DELAY, "send": b"\r"},
        {"await": DIALOG_RE, "timeout": CONFIRM_TIMEOUT, "send": b"\r"},
    ]
    if continue_after:
        steps.append({"await_idle": AFTER_CONFIRM_IDLE, "send": (CONTINUE_MSG + "\r").encode()})
    return steps


def build_continue_sequence():
    return [{"await_idle": IDLE_SECONDS, "send": (CONTINUE_MSG + "\r").encode()}]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_file(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def banner(msg):
    line = "\r\n\x1b[36m[keeper]\x1b[0m %s\r\n" % msg
    try:
        os.write(2, line.encode())
    except OSError:
        pass
    _log_file(msg)


# ---------------------------------------------------------------------------
# Supervisor: scans output and runs recovery sequences.
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self):
        self.buffer = ""
        self.chain_index = 0
        self.last_output = time.time()
        self.last_user_input = 0.0
        self.cooldown_until = 0.0
        self.pending = None          # None or ("wait", resume_at)
        self.episode_output = 0
        self.seq = None              # list of steps, or None
        self.seq_i = 0
        self.step_start = 0.0
        self.seq_target_index = 0
        self.seq_label = ""

    def now(self):
        return time.time()

    # -- output handling ----------------------------------------------------

    def on_output(self, data):
        self.last_output = self.now()
        text = ANSI_RE.sub(b"", data).decode("utf-8", "replace")
        self.buffer = (self.buffer + text)[-8000:]

        if self.seq is not None:
            return

        if self.pending is not None and self.pending[0] == "wait":
            self.episode_output += len(text.strip())
            if self.episode_output > 400:
                banner("session activity resumed, cancelling the pending continue.")
                self.pending = None
                self.cooldown_until = self.now() + COOLDOWN_SECONDS
            return

        if self.now() < self.cooldown_until:
            return

        if LIMIT_RE.search(self.buffer):
            self._begin_episode()

    def on_user_input(self, data):
        self.last_user_input = self.now()

    # -- episode start ------------------------------------------------------

    def _begin_episode(self):
        self.episode_output = 0
        action, nxt = decide_on_limit(self.chain_index, len(CHAIN), SWITCH_ENABLED, WAIT_ENABLED)
        if action == "switch":
            target = CHAIN[nxt]
            banner("%s usage limit reached. Switching to %s and continuing."
                   % (CHAIN[self.chain_index], target))
            self._start_sequence(build_switch_sequence(target), nxt, "switch to %s" % target)
        elif action == "wait":
            reset = parse_reset_epoch(self.buffer)
            cap = self.now() + WAIT_CAP_MINUTES * 60
            if reset and reset > self.now():
                resume_at = min(reset + RESET_BUFFER_SECONDS, cap)
                banner("every model is limited. Waiting until %s to resume."
                       % time.strftime("%H:%M:%S", time.localtime(resume_at)))
            else:
                resume_at = min(self.now() + RETRY_MINUTES * 60, cap)
                banner("limit reached but no reset time was visible. Will retry at %s."
                       % time.strftime("%H:%M:%S", time.localtime(resume_at)))
            self.pending = ("wait", resume_at)
        else:
            banner("limit reached but switching and waiting are both disabled. Not acting.")
            self.cooldown_until = self.now() + COOLDOWN_SECONDS

    def _start_sequence(self, steps, target_index, label):
        self.buffer = ""          # scan only output produced from here on
        self.seq = steps
        self.seq_i = 0
        self.step_start = self.now()
        self.seq_target_index = target_index
        self.seq_label = label
        _log_file("recovery start: %s" % label)

    # -- driving the loop ---------------------------------------------------

    def _idle(self, secs):
        t = self.now()
        return (t - self.last_output) >= secs and (t - self.last_user_input) >= secs

    def tick(self, write):
        if self.seq is not None:
            self._run_step(write)
            return

        if self.pending is not None and self.pending[0] == "wait":
            resume_at = self.pending[1]
            if self.now() >= resume_at and self._idle(IDLE_SECONDS):
                banner("reset window passed. Resuming on %s." % CHAIN[0])
                self.pending = None
                if len(CHAIN) > 1:
                    self._start_sequence(build_switch_sequence(CHAIN[0]), 0, "resume on %s" % CHAIN[0])
                else:
                    self._start_sequence(build_continue_sequence(), 0, "resume")

    def _run_step(self, write):
        step = self.seq[self.seq_i]
        t = self.now()
        ready = False

        if "await_idle" in step:
            ready = self._idle(step["await_idle"])
        elif "delay" in step:
            ready = (t - self.step_start) >= step["delay"]
        elif "await" in step:
            if step["await"].search(self.buffer):
                ready = True
            elif (t - self.step_start) >= step.get("timeout", 5.0):
                ready = True   # fallback: send anyway, a stray Enter is harmless

        if not ready:
            return

        payload = step.get("send")
        if payload:
            if DRYRUN:
                _log_file("would send: %r" % payload)
            else:
                write(payload)
        self._advance()

    def _advance(self):
        self.seq_i += 1
        self.step_start = self.now()
        if self.seq_i >= len(self.seq):
            self.chain_index = self.seq_target_index
            self.seq = None
            self.cooldown_until = self.now() + COOLDOWN_SECONDS
            banner("recovery complete (%s). Now on %s.%s"
                   % (self.seq_label, CHAIN[self.chain_index], " (dry run)" if DRYRUN else ""))


# ---------------------------------------------------------------------------
# The pseudo terminal driver.
# ---------------------------------------------------------------------------

def _set_winsize(master_fd):
    try:
        packed = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    except (OSError, ValueError):
        pass


def build_child_args(argv):
    args = list(argv)
    has_model = any(a in ("--model", "-m") or a.startswith("--model=") for a in args)
    if FORCE_MODEL and not has_model and CHAIN:
        args = ["--model", CHAIN[0]] + args
    return args


def main(argv):
    child_args = build_child_args(argv)

    stdin_is_tty = sys.stdin.isatty()
    old_attrs = None

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(CLAUDE_BIN, [CLAUDE_BIN] + child_args)
        except OSError as exc:
            sys.stderr.write("keeper: could not launch %s: %s\n" % (CLAUDE_BIN, exc))
            os._exit(127)

    banner("supervising claude. chain: %s. switch=%s wait=%s%s"
           % (" then ".join(CHAIN), SWITCH_ENABLED, WAIT_ENABLED, " dryrun" if DRYRUN else ""))

    if stdin_is_tty:
        try:
            old_attrs = termios.tcgetattr(0)
            tty.setraw(0)
        except termios.error:
            old_attrs = None

    _set_winsize(master_fd)
    signal.signal(signal.SIGWINCH, lambda *_: _set_winsize(master_fd))

    sup = Supervisor()

    def write_master(payload):
        try:
            os.write(master_fd, payload)
        except OSError:
            pass

    watch_stdin = True
    exit_code = 0
    try:
        while True:
            fds = [master_fd]
            if watch_stdin:
                fds.append(0)
            try:
                rlist, _, _ = select.select(fds, [], [], 0.1)
            except select.error:
                continue

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(1, data)
                sup.on_output(data)

            if watch_stdin and 0 in rlist:
                try:
                    user = os.read(0, 65536)
                except OSError:
                    user = b""
                if not user:
                    watch_stdin = False
                else:
                    write_master(user)
                    sup.on_user_input(user)

            sup.tick(write_master)
    finally:
        if old_attrs is not None:
            try:
                termios.tcsetattr(0, termios.TCSAFLUSH, old_attrs)
            except termios.error:
                pass
        try:
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                exit_code = 128 + os.WTERMSIG(status)
        except OSError:
            pass

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
