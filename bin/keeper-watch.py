#!/usr/bin/env python3
"""
keeper-watch: transparent supervision of a live Claude Code session.

It launches the real claude inside a pseudo terminal and forwards the terminal
through unchanged, so the session looks and feels exactly like running claude
directly. In the background it watches the session output for a usage limit and
tells two kinds apart:

  Fable credit limit  (for example "You're out of usage credits", "Fable 5
  limit") means only Fable is exhausted while the session still has room. Here
  it switches to the fallback model (the latest Opus) with the /model command,
  confirms the switch, and sends a continue.

  Session limit  (for example "usage limit reached", "weekly limit", "5-hour
  session limit") means the whole session is spent, so switching would not help.
  Here it keeps the selected model, waits for the session to refresh, then
  resumes on the selected model.

The real terminal UI positions text with cursor moves instead of spaces, so the
plain text scan can see words run together. The patterns allow optional
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
# Configuration
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


# ---------------------------------------------------------------------------
# Limit detection and classification
#
# Two families of banner, taken from the real Claude Code messages. A Fable
# credit limit can be worked around by switching model; a session limit cannot.
# ---------------------------------------------------------------------------

FABLE_LIMIT_PATTERN = os.environ.get("CLAUDEKEEPER_FABLE_LIMIT_PATTERN", (
    r"reached\s*your\s*fable(?:\s*5)?\s*limit"
    r"|out\s*of\s*usage\s*credits"
    r"|usage\s*credit\s*limit\s*reached"
    r"|monthly\s*spend\s*limit"
    r"|\bfable\s*5\s*limit\b"
))

SESSION_LIMIT_PATTERN = os.environ.get("CLAUDEKEEPER_SESSION_LIMIT_PATTERN", (
    r"usage\s*limit\s*reached"
    r"|weekly\s*limit\s*reached"
    r"|(?:5|five)\W?hour\s*(?:session\s*)?limit\s*reached"
    r"|reached\s*your\s*(?:weekly|usage|session)\s*limit"
    r"|hit\s*your\s*(?:weekly|usage|session)[^.\n]{0,20}limit"
    r"|session\s*limit\s*reached"
))

FABLE_LIMIT_RE = re.compile(FABLE_LIMIT_PATTERN, re.I)
SESSION_LIMIT_RE = re.compile(SESSION_LIMIT_PATTERN, re.I)
# Kept for compatibility and simple "is this any limit" checks.
LIMIT_RE = re.compile("(?:%s)|(?:%s)" % (FABLE_LIMIT_PATTERN, SESSION_LIMIT_PATTERN), re.I)

# The confirmation shown after /model, for example "Switch model? Yes, switch".
DEFAULT_DIALOG_PATTERN = r"switch\s*model|yes,?\s*switch|switch\s*to\s*(?:opus|sonnet|fable|haiku)"
DIALOG_RE = re.compile(os.environ.get("CLAUDEKEEPER_DIALOG_PATTERN", DEFAULT_DIALOG_PATTERN), re.I)

# Strip terminal control sequences so the text scan sees words, not escapes.
ANSI_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]")


def classify_limit(text):
    """Return 'fable', 'session', or None."""
    if FABLE_LIMIT_RE.search(text):
        return "fable"
    if SESSION_LIMIT_RE.search(text):
        return "session"
    return None


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
# Reset time parsing. Handles "resets in <duration>", "resets at <time>", a
# numeric or ISO field, and the pipe format.
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)


def parse_duration_seconds(s):
    total = 0
    found = False
    for val, unit in _DUR_RE.findall(s):
        found = True
        v = int(val)
        u = unit.lower()
        if u.startswith("d"):
            total += v * 86400
        elif u.startswith("h"):
            total += v * 3600
        elif u.startswith("s"):
            total += v
        else:  # minutes
            total += v * 60
    return total if found else None


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

    # "resets in 2h 30m", "resets in 45 minutes", "reset in 3 hours"
    m = re.search(r"resets?\s+in\s+([^.\n]{1,40})", text, re.I)
    if m:
        secs = parse_duration_seconds(m.group(1))
        if secs is not None:
            return now + secs

    # "resets at 3pm", "reset at 15:00"
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


# Kept for the test suite and Node parity.
def decide_on_limit(chain_index, chain_length, switch_enabled, wait_enabled):
    if switch_enabled and chain_index < chain_length - 1:
        return ("switch", chain_index + 1)
    if wait_enabled:
        return ("wait", 0)
    return ("stop", chain_index)


# ---------------------------------------------------------------------------
# Recovery step sequences
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


def _clock(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self, selected_model, fallback_model):
        self.selected_model = selected_model
        self.fallback_model = fallback_model
        self.current_model = selected_model
        self.buffer = ""
        self.last_output = time.time()
        self.last_user_input = 0.0
        self.cooldown_until = 0.0
        self.pending = None          # None, ("settle", t0), or ("wait", resume_at)
        self.seq = None
        self.seq_i = 0
        self.step_start = 0.0
        self.seq_target_model = None
        self.seq_label = ""

    def now(self):
        return time.time()

    # -- output handling ----------------------------------------------------

    def on_output(self, data):
        self.last_output = self.now()
        text = ANSI_RE.sub(b"", data).decode("utf-8", "replace")
        self.buffer = (self.buffer + text)[-8000:]

        # While already handling a limit, keep buffering but do not re-detect.
        if self.seq is not None or self.pending is not None:
            return

        if self.now() < self.cooldown_until:
            return

        kind = classify_limit(self.buffer)
        if kind is not None:
            self._begin_episode(kind)

    def on_user_input(self, data):
        self.last_user_input = self.now()
        # If the human comes back and types while we are waiting or about to act,
        # stand down and let them drive.
        if self.pending is not None and data:
            banner("you are interacting with the session, standing down until the next limit.")
            self.pending = None
            self.cooldown_until = self.now() + max(COOLDOWN_SECONDS, 90)

    # -- deciding what to do ------------------------------------------------

    def _can_switch_to_fallback(self):
        return (
            SWITCH_ENABLED
            and self.fallback_model
            and self.current_model.lower() == self.selected_model.lower()
            and self.fallback_model.lower() != self.current_model.lower()
        )

    def _begin_episode(self, kind):
        if kind == "fable" and self._can_switch_to_fallback():
            banner("Fable limit reached. Switching to %s to keep working within this session."
                   % self.fallback_model)
            self._start_sequence(build_switch_sequence(self.fallback_model),
                                 self.fallback_model, "switch to %s" % self.fallback_model)
            return

        if not WAIT_ENABLED:
            banner("limit reached and waiting is disabled. Not acting.")
            self.cooldown_until = self.now() + COOLDOWN_SECONDS
            return

        # Do not switch models. Keep the selected model and let the banner finish
        # rendering before reading the reset time, then wait for the refresh.
        label = "session limit" if kind == "session" else "limit"
        banner("%s reached. Keeping %s and reading the reset time." % (label, self.selected_model))
        self.pending = ("settle", self.now())

    def _start_sequence(self, steps, target_model, label):
        self.buffer = ""
        self.seq = steps
        self.seq_i = 0
        self.step_start = self.now()
        self.seq_target_model = target_model
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

        if self.pending is None:
            return

        kind = self.pending[0]
        if kind == "settle":
            t0 = self.pending[1]
            # Wait for the banner to finish printing, then read the reset time.
            if self._idle(IDLE_SECONDS) or (self.now() - t0) > 12:
                reset = parse_reset_epoch(self.buffer)
                cap = self.now() + WAIT_CAP_MINUTES * 60
                if reset and reset > self.now():
                    resume_at = min(reset + RESET_BUFFER_SECONDS, cap)
                    when = _clock(resume_at)
                else:
                    resume_at = min(self.now() + RETRY_MINUTES * 60, cap)
                    when = _clock(resume_at) + " (no reset time seen)"
                banner("waiting until %s for the session to refresh, then resuming on %s."
                       % (when, self.selected_model))
                self.buffer = ""   # do not re-match this same banner later
                self.pending = ("wait", resume_at)

        elif kind == "wait":
            resume_at = self.pending[1]
            if self.now() >= resume_at and self._idle(IDLE_SECONDS):
                banner("session refreshed. Resuming on %s." % self.selected_model)
                self.pending = None
                if self.current_model.lower() != self.selected_model.lower() and SWITCH_ENABLED:
                    # We had switched to the fallback earlier; return to the selected model.
                    self._start_sequence(build_switch_sequence(self.selected_model),
                                         self.selected_model, "resume on %s" % self.selected_model)
                else:
                    self._start_sequence(build_continue_sequence(),
                                         self.current_model, "resume")

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
                ready = True   # fallback: a stray Enter on an empty composer is harmless

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
            if self.seq_target_model:
                self.current_model = self.seq_target_model
            self.seq = None
            self.cooldown_until = self.now() + COOLDOWN_SECONDS
            banner("recovery complete (%s). Now on %s.%s"
                   % (self.seq_label, self.current_model, " (dry run)" if DRYRUN else ""))


# ---------------------------------------------------------------------------
# The pseudo terminal driver
# ---------------------------------------------------------------------------

def _set_winsize(master_fd):
    try:
        packed = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    except (OSError, ValueError):
        pass


def _extract_user_model(argv):
    for i, a in enumerate(argv):
        if a in ("--model", "-m") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


def build_child_args(argv, selected_model):
    args = list(argv)
    has_model = _extract_user_model(argv) is not None
    if FORCE_MODEL and not has_model and selected_model:
        args = ["--model", selected_model] + args
    return args


def main(argv):
    user_model = _extract_user_model(argv)
    selected_model = user_model or (CHAIN[0] if CHAIN else PRIMARY)
    fallback_model = next((m for m in FALLBACKS if m.lower() != selected_model.lower()), None)

    child_args = build_child_args(argv, selected_model)

    stdin_is_tty = sys.stdin.isatty()
    old_attrs = None

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(CLAUDE_BIN, [CLAUDE_BIN] + child_args)
        except OSError as exc:
            sys.stderr.write("keeper: could not launch %s: %s\n" % (CLAUDE_BIN, exc))
            os._exit(127)

    banner("supervising claude. selected=%s, fallback=%s, switch=%s wait=%s%s"
           % (selected_model, fallback_model or "none", SWITCH_ENABLED, WAIT_ENABLED,
              " dryrun" if DRYRUN else ""))

    if stdin_is_tty:
        try:
            old_attrs = termios.tcgetattr(0)
            tty.setraw(0)
        except termios.error:
            old_attrs = None

    _set_winsize(master_fd)
    signal.signal(signal.SIGWINCH, lambda *_: _set_winsize(master_fd))

    sup = Supervisor(selected_model, fallback_model)

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
