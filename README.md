# helm

A terminal dashboard for the servers you keep coding-agent sessions on.
Reads **Claude Code** and **opencode**.

Named for where you stand to watch every station at once.

Installed as `helm` -- the command is `helm`. The repository is named `helm-tui` only so it
does not collide with the Kubernetes package manager, which this is unrelated to.

It lists every `tmux` session across every host in your `~/.ssh/config`, tells you **which ones are
working and which are waiting on you**, shows each session's live screen, and lets you reply to a
chat without attaching to it.

```
        chat                 │ web-01/claude
 1   !  web-01/claude        │ unsent draft  commit and push both
 2   *  db-01/shell          │ attached  -  4m ago  -  live
                             │
                             │ SCREEN
                             │ ● Restructured the Jobs column header into two rows:
                             │ ✻ Cooked for 24m 14s
                             │ ❯ commit and push both
                             │
                             │ enter  reply below   o  open it here   w  new window
```

## Why

If you run coding agents on remote boxes, the question is never "which server" — it's *"is that chat
still working, or has it been sitting waiting for me for an hour?"*. Nothing answers that from a
distance, so this reads it off the screen.

## Chat states

| Mark | State | Means |
|---|---|---|
| `*` | working | Busy, or running background agents. Leave it. |
| `!` | needs you | Blocked on a permission prompt. |
| `!` | unsent draft | Text left in the box, never submitted — looks done, isn't. |
| `o` | idle | Empty prompt, waiting. |
| `.` | shell | Not an agent session — a plain shell. |

**How it works, and its limit.** There is no API for this. `agent_state.py` reads the session's
visible screen from `tmux capture-pane` and matches markers. The states above are shared; the
markers are per-agent, because the two draw nothing alike:

| | Claude Code | opencode |
|---|---|---|
| busy | `esc to interrupt` | `esc interrupt` in the footer |
| blocked | `Do you want to…`, `1. Yes` | `Permission required`, `Allow once / Reject` |
| the input box | between the last two horizontal rules | the `┃` run above the `╹▀▀▀` rule |
| empty box | the `Try "…"` placeholder | the `Ask anything…` placeholder |
| context used | `275.1k tokens` | `7.4K (3%)` in the footer |

Which agent a session is running comes from the pane's command, so a shell is settled without
guessing from pixels. Background agent fleets (`N/M agents done`) are read for Claude Code only —
opencode's equivalent has not been captured yet, so a session of its own is never reported as busy
on that basis.

Anything unrecognised reports **unknown**, never idle: claiming a blocked chat is idle is the one
failure that would make the tool worse than not looking.

A redesign on either side can break these markers. `tests/test_agent_state.py` runs the reader
against real captured screens — `python3 tests/test_agent_state.py`, no test dependencies — so a
break shows up as a failure rather than as a quietly wrong dashboard.

Adding a third agent means writing its patterns as a class in `agent_state.py`; the callers do not
change.

## Live, not polled

One held-open `ssh` channel per host runs a loop that re-dumps each session's screen and only sends
anything when it changed. Polling pays the network round trip on every check; this pays it once and
then streams. An idle host costs nothing. A 45-second sweep covers what the stream doesn't: uptime,
disk, mounts, and the session list.

`HELM_NO_WATCH=1` falls back to plain polling.

## Keys

| Key | Does |
|---|---|
| `enter` | Reply to the selected chat, in place |
| `enter` (empty box) | Submit the draft already sitting in that chat |
| `o` / `w` | Open it in this window / in its own window |
| `F12` | Leave a session, back to the list (`ctrl-b d` also works) |
| `1`-`9` | Select a chat |
| `s` / `v` / `/` | Sidebar / switch view / filter |
| `n` `r` `f` `u` `a` `k` `e` | New shell, refresh, mount, unmount, add host, install key, edit config |
| `?` | Full key guide, in the app |

## Replying safely

Text is sent as a **tmux buffer**, never interpolated into a command line:

```
tmux load-buffer -b reply -  &&  tmux paste-buffer -t <session> -d  &&  tmux send-keys Enter
```

So quotes, `$VAR`, backticks and semicolons arrive verbatim and nothing you type can become a remote
command. `Enter` is a separate keystroke on purpose — pasted as part of the buffer it lands as a
literal newline inside the agent's input box instead of submitting.

## Install

Requires Python 3.11+ locally, and `tmux` on the hosts you connect to.

```bash
mkdir -p ~/.local/share/helm/app
cp *.py ~/.local/share/helm/app/
cp bin/* ~/.local/bin/
python3 -m venv ~/.local/share/helm/venv
~/.local/share/helm/venv/bin/pip install -r requirements.txt
helm --check      # probe every host once and print what it found
```

Colours follow the [Omarchy](https://omarchy.org) desktop theme when present, and fall back to a
built-in palette otherwise. `HELM_THEME=light|dark` forces it.

## Notes for tiling WMs

Windows label themselves in tmux's status bar (`[web-01/claude]`) because tiling compositors
generally have no titlebars. Under Hyprland's `dwindle` layout a new window splits whichever window
has focus, along its longer axis — so a tall panel gets halved. A column layout (`scrolling`, or
`master`) keeps the list a full-height column.

## Licence

MIT.
