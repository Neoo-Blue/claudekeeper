#!/usr/bin/env python3
"""
keeper-watch: transparent supervision of a live Claude Code session.

It launches the real claude inside a pseudo terminal and forwards the terminal
through unchanged, so the session looks and feels exactly like running claude
directly. In the background it watches the session output for a usage limit.
When one appears it either switches to the next model in the chain (for example
Fable to the latest Opus) with the /model command and sends a continue, or, when
every model is limited, waits for the reset window and then sends a continue.

No third party dependencies: only the Python standard library.
"""

import os
import sys
import re
import time
import signal
import select
import struct
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
RETRY_MINUTES = _num("CLAUDEKEEPER_RETRY_MINUTES", 20)
RESET_BUFFER_SECONDS = _num("CLAUDEKEEPER_RESET_BUFFER_SECONDS", 20)
COOLDOWN_SECONDS = _num("CLAUDEKEEPER_COOLDOWN_SECONDS", 25)
WAIT_CAP_MINUTES = _num("CLAUDEKEEPER_WAIT_CAP_MINUTES", 360)
CLAUDE_BIN = os.environ.get("CLAUDEKEEPER_CLAUDE_BIN", "claude")
LOG_PATH = os.environ.get(
    "CLAUDEKEEPER_WATCH_LOG",
    os.path.join(os.path.expanduser("~"), ".claude", "claudekeeper-watch.log"),
)

DEFAULT_LIMIT_PATTERN = (
    r"usage limit reached"
    r"|you(?:'|’)?ve hit your .{0,40}limit"
    r"|(?:5|five)[ -]hour limit reached"
    r"|weekly limit reached"
    r"|reached your .{0,30}usage limit"
)
LIMIT_RE = re.compile(os.environ.get("CLAUDEKEEPER_LIMIT_PATTERN", DEFAULT_LIMIT_PATTERN), re.I)

# Strip terminal control sequences so the text scan sees plain words.
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
# The decision for what to do when a limit is seen. Pure, so it is easy to test.
# ---------------------------------------------------------------------------

def decide_on_limit(chain_index, chain_length, switch_enabled, wait_enabled):
    if switch_enabled and chain_index < chain_length - 1:
        return ("switch", chain_index + 1)
    if wait_enabled:
        return ("wait", 0)
    return ("stop", chain_index)


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
# Supervisor state: scans output and schedules recovery actions.
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self):
        self.buffer = ""
        self.chain_index = 0
        self.last_output = time.time()
        self.last_user_input = 0.0
        self.cooldown_until = 0.0
        # pending is None, ("switch",), or ("wait", resume_at)
        self.pending = None
        self.episode_output = 0  # bytes of output seen since a limit was detected
        # queue of (send_at, bytes) injections
        self.queue = []

    def now(self):
        return time.time()

    def on_output(self, data):
        self.last_output = self.now()
        text = ANSI_RE.sub(b"", data).decode("utf-8", "replace")
        self.buffer = (self.buffer + text)[-8000:]

        if self.pending is not None:
            # If the session clearly resumed on its own, drop what we planned.
            self.episode_output += len(text.strip())
            if self.episode_output > 400 and self.pending[0] == "wait":
                # Substantial new activity during a wait means someone continued.
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

    def _begin_episode(self):
        self.episode_output = 0
        action, nxt = decide_on_limit(self.chain_index, len(CHAIN), SWITCH_ENABLED, WAIT_ENABLED)
        if action == "switch":
            target = CHAIN[nxt]
            banner("%s usage limit reached. Will switch to %s and continue once the session is idle."
                   % (CHAIN[self.chain_index], target))
            self.pending = ("switch",)
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

    def _idle(self):
        t = self.now()
        return (t - self.last_output) >= IDLE_SECONDS and (t - self.last_user_input) >= IDLE_SECONDS

    def _enqueue_recovery(self, model_cmd, note):
        t = self.now()
        if model_cmd:
            self.queue.append((t + 0.1, ("/model %s\r" % model_cmd).encode()))
            self.queue.append((t + 1.3, (CONTINUE_MSG + "\r").encode()))
        else:
            self.queue.append((t + 0.1, (CONTINUE_MSG + "\r").encode()))
        _log_file("enqueued recovery: %s" % note)

    def tick(self, write):
        t = self.now()

        # Flush any due injections.
        if self.queue:
            still = []
            for send_at, payload in self.queue:
                if t >= send_at:
                    if not DRYRUN:
                        write(payload)
                else:
                    still.append((send_at, payload))
            self.queue = still

        if self.pending is None:
            return

        kind = self.pending[0]
        if kind == "switch":
            if self._idle():
                target = CHAIN[self.chain_index + 1]
                banner("switching to %s and continuing.%s" % (target, " (dry run)" if DRYRUN else ""))
                self._enqueue_recovery(target, "switch to %s" % target)
                self.chain_index += 1
                self.cooldown_until = t + COOLDOWN_SECONDS
                self.pending = None
        elif kind == "wait":
            resume_at = self.pending[1]
            if t >= resume_at and self._idle():
                banner("reset window passed. Resuming on %s.%s" % (CHAIN[0], " (dry run)" if DRYRUN else ""))
                model_cmd = CHAIN[0] if len(CHAIN) > 1 else None
                self._enqueue_recovery(model_cmd, "wait then resume on %s" % CHAIN[0])
                self.chain_index = 0
                self.cooldown_until = t + COOLDOWN_SECONDS
                self.pending = None


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
        # Child: become the real claude.
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
