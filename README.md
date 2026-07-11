# claudekeeper

Keep a Claude Code task alive across usage limits.

Long autonomous runs stop the moment a usage limit is reached, and you have to
come back, notice, and resume by hand. claudekeeper is a small supervisor that
sits around Claude Code and does that for you. When a run stops because a usage
limit was reached it does one of two things:

1. **Switches models.** By default the chain is `fable` then `opus`. When the
   Fable limit is reached, it switches to the latest Opus and resumes the same
   session right away, so the work keeps moving.
2. **Waits for reset.** When every model in the chain is limited, it reads the
   reset time from Claude Code and waits, then resumes on the primary model.

Either way, the same session continues where it left off. You do not babysit it.

## Why a wrapper is needed

Claude Code already has `--fallback-model`, but that only triggers on overload
or a model that is unavailable. Usage limits surface immediately and are not
covered by it. claudekeeper fills that gap by watching for the limit and driving
the resume itself.

## Requirements

- The `claude` CLI on your PATH (Claude Code).
- Node 18 or newer.
- Python 3 for transparent mode only (ships with macOS and most Linux).
- No third party packages.

## Install

```sh
git clone https://github.com/Neoo-Blue/claudekeeper.git
cd claudekeeper
./install.sh
```

`install.sh` links `bin/claudekeeper.js` into `~/.local/bin/claudekeeper`. Make
sure `~/.local/bin` is on your PATH.

## Transparent mode (regular sessions)

If you want your normal interactive sessions supervised without thinking about
it, wrap the `claude` command once:

```sh
claudekeeper install-shell --write   # adds a claude() function to your shell profile
```

Open a new shell and keep using `claude` exactly as before. The session looks
and feels identical, but a hidden supervisor watches it in the background. When
a usage limit is reached it switches Fable to the latest Opus and sends a
continue, or, when every model is limited, waits for the reset window and then
continues. Turn it off any time:

```sh
claudekeeper uninstall-shell
```

Under the hood the wrapper runs `claudekeeper watch`, which launches the real
`claude` inside a pseudo terminal and passes the terminal straight through. It
needs Python 3, which ships with macOS and most Linux systems. A few notes:

- The session starts on the primary model (Fable by default) so the chain is
  predictable. Set `CLAUDEKEEPER_FORCE_MODEL=0` to keep your own default, or
  pass your own `-m` when launching.
- The mid session model switch drives the `/model` command in the session, so it
  is best effort. The wait and continue is reliable.
- Try it without any typing into your session first: `claudekeeper watch --dry-run`
  logs what it would do instead of doing it.
- Actions only fire when the session has gone quiet, so it does not fight you
  while you are typing. Everything it does is logged to
  `~/.claude/claudekeeper-watch.log`.

## Usage

Start an unattended task:

```sh
claudekeeper run "Refactor the billing module and run the tests"
```

Keep going across many turns until the work is done:

```sh
claudekeeper run --keep-going 20 "Work through the TODO list until it is empty"
```

Resume a session that already stopped:

```sh
claudekeeper list                 # find the session id
claudekeeper resume <session-id>  # or: claudekeeper resume --last
```

## How the model chain works

The chain is the primary model followed by the fallbacks:

```sh
claudekeeper run "..."                      # fable then opus (default)
claudekeeper run -m opus "..."              # opus only, wait on limit
claudekeeper run --fallback opus,sonnet ... # fable then opus then sonnet
claudekeeper run --no-fallback "..."        # never switch, only wait
```

When a model hits its usage limit and a later model in the chain is still
available, claudekeeper switches to it and resumes. When the last model is also
limited, it waits for the reset window, then starts the chain again from the
primary model. Use `opus` (the alias) so you always land on the latest Opus.

## Options

```
run [prompt]                     start a supervised task
resume <id|--last>               resume an existing session and keep it alive
list                             show recent sessions to pick from

-m, --model <name>               primary model (default fable)
    --fallback a,b               fallback chain (default opus)
    --no-fallback                never switch models, only wait for reset
    --no-switch                  always wait instead of switching
    --no-wait                    never wait, stop once every model is limited
    --keep-going N               after a turn finishes, auto continue up to N times
    --continue-prompt <s>        text used to resume after a limit
-c, --cwd <dir>                  working directory for the task
    --permission-mode <m>        claude permission mode, for example acceptEdits
    --dangerously-skip-permissions   pass through to claude (alias: --yes)
    --add-dir <dir>              extra directory claude may access (repeatable)
    --retry-minutes N            wait when a reset time cannot be read (default 20)
    --wait-cap-minutes N         never wait longer than this at once (default 360)
    --max-resumes N              stop after this many resumes (default 200)
    --quiet                      only show keeper logs, not the streamed output
    -- <args>                    everything after -- is passed straight to claude
```

## Environment

Defaults can be set with environment variables:

```
CLAUDEKEEPER_PRIMARY_MODEL       default primary model (default fable)
CLAUDEKEEPER_FALLBACK_MODELS     comma list of fallbacks (default opus)
CLAUDEKEEPER_RETRY_MINUTES       wait when reset time is unknown (default 20)
CLAUDEKEEPER_WAIT_CAP_MINUTES    cap on any single wait (default 360)
CLAUDEKEEPER_MAX_RESUMES         resume cap (default 200)
CLAUDEKEEPER_CLAUDE_BIN          path to the claude binary (default claude)
```

## What it does not do

- It does not bypass or extend your plan limits. It waits them out or moves to a
  model you still have room on.
- It stops on hard failures that a resume cannot fix, such as an invalid API key
  or an empty credit balance, and tells you why.

## Tests

```sh
npm test
```

The unit tests cover limit detection, reset time parsing, and the model switch
logic. The integration tests drive the full loop against a fake claude, so they
verify switching and waiting without spending any quota.

## License

MIT. See [LICENSE](LICENSE).
