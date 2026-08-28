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

## This machine, too

The first host in the list is `local` -- the laptop the panel is running on. It is not ssh to
yourself: the probe, the watcher and the replies are the same POSIX shell scripts handed straight
to `bash` instead of to `ssh`, so a local row needs no sshd, no key and no round trip. One function
in `hosts.py` knows the difference; nothing else does.

**It reads tmux, not terminals.** A `claude` running in a bare terminal window cannot be read by
anything without attaching to it, and it dies when you close the window -- so it is not listed.
Start local agents the way `ssh-connect` starts remote ones:

```
tmux new -A -s claude
```

and they show up here, survive the window closing, and can be replied to from the panel.

`HELM_NO_LOCAL=1` hides the row. A `Host local` already in `~/.ssh/config` wins: an alias you can
really ssh to beats one the panel invented.

**If helm is itself running inside tmux**, a local session opens in its own window instead of taking
this one. It has to: tmux gives one terminal to one client, so attaching in place detaches the
session helm is running in -- helm disappears, and F12 then closes the window rather than bringing
it back, because the client it detaches is the outer one. Remote sessions are a different tmux
server and still take the terminal as before.

For the same reason the session helm is running in is left out of its own list: attaching to it is
the one attach that cannot go anywhere.

## In the window

```
  ⎈  ╻ ╻┏━╸╻  ┏┳┓        1  o  shell        idle
     ┣━┫┣╸ ┃  ┃┃┃        2  !  claude       needs you
     ╹ ╹┗━╸┗━╸╹ ╹        3  *  shell        working
     2 waiting on you    ...
                         ?  6/7 hosts · 12 sessions
```

U+2388 is, literally, the helm symbol. The `?` at the foot of the list has the marks, the keys and
who wrote it.

## Keys and actions

| | Does |
|---|---|
| `alt-1`..`alt-9` | Open the session with that number in the list |
| `ctrl-tab` / `ctrl-shift-tab` | Next / previous open session |
| `F12` | Back to the list -- arrows move, enter opens |
| `ctrl-shift-w` | Close the view; the session behind it keeps running |
| `F11` | Fullscreen, and back |
| `ctrl-q` | Quit |
| right-click a session | Open it, or kill it (asked first -- there is no undo) |
| `+` | New session on the selected host, local or remote |
| server icon | Add a server to `~/.ssh/config` |
| `i` | What the marks mean, and the keys |

Every session carries the number that opens it, so the shortcut is never counted out. Past nine the
column is blank rather than promising a key that does not exist.

## Keys (`--tui`)

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

## The one password

helm never sees it. Your ssh key is unlocked once -- by the desktop keyring at login -- and every
`ssh` it spawns rides the agent, which is why no host prompts you. If the agent is holding nothing,
the foot of the list says so and offers to run `ssh-add` in a terminal of its own: the passphrase
goes from your keyboard to `ssh-add`, never through helm.

Right-click a host for **passwords and keys** -- whatever else you keep for it: a database password,
an API key. They live in the desktop keyring, which the same login password already unlocks, so
"one password for all of it" is the arrangement that exists rather than a thing to build. helm
stores nothing itself: a value is fetched when you press Show, masked again on Hide, and a copy is
wiped off the clipboard after thirty seconds.

## Replying safely

Text is sent as a **tmux buffer**, never interpolated into a command line:

```
tmux load-buffer -b reply -  &&  tmux paste-buffer -t <session> -d  &&  tmux send-keys Enter
```

So quotes, `$VAR`, backticks and semicolons arrive verbatim and nothing you type can become a remote
command. `Enter` is a separate keystroke on purpose — pasted as part of the buffer it lands as a
literal newline inside the agent's input box instead of submitting.

## Install

Requires Python 3.11+, GTK4 and VTE locally, and `tmux` on the hosts you connect to.

```bash
sudo pacman -S gtk4 vte4 python-gobject          # the window
mkdir -p ~/.local/share/helm/app
cp *.py ~/.local/share/helm/app/
cp bin/* ~/.local/bin/
python3 -m venv --system-site-packages ~/.local/share/helm/venv
~/.local/share/helm/venv/bin/pip install -r requirements.txt
helm --check      # probe every host once and print what it found
```

`--system-site-packages` is not optional: PyGObject is a system package, and a venv sealed off from
it cannot see GTK at all.

`cp helm.desktop ~/.local/share/applications/` if you want it in a launcher. It is a window now, so
starting it through a TUI wrapper (`omarchy-launch-tui helm`, or a terminal binding) leaves an empty
terminal sitting beside the real one -- the terminal is hosting a process that no longer draws
anything in it.

`helm --tui` is the terminal panel this grew out of. It is what you want over ssh, where there is
no window to open, and it needs no GTK.

Colours follow the [Omarchy](https://omarchy.org) desktop theme when present, and fall back to a
built-in palette otherwise. `HELM_THEME=light|dark` forces it.

## One window

The list is a sidebar and the session opens beside it, in a real terminal widget -- VTE, the one
GNOME Terminal and Ptyxis are built on. Opening a chat costs you nothing you were already looking
at.

It used to throw a new window at the compositor per session, which is not a layout: on Hyprland
those landed on whichever workspace happened to be active, at whatever size dwindle felt like,
while the panel sat on another workspace entirely.

tmux is still doing the real work on the far side -- it is what makes a session survive the window
closing. You just stop seeing it as a window of its own.

## Notes for tiling WMs

Windows label themselves in tmux's status bar (`[web-01/claude]`) because tiling compositors
generally have no titlebars. Under Hyprland's `dwindle` layout a new window splits whichever window
has focus, along its longer axis — so a tall panel gets halved. A column layout (`scrolling`, or
`master`) keeps the list a full-height column.

## Licence

MIT.
