# helm

A window for the servers you keep coding-agent sessions on.
Reads **Claude Code** and **opencode**.

Named for where you stand to watch every station at once.

Installed as `helm` -- the command is `helm`. The repository is named `helm-tui` for the terminal
panel this began as, and to stay out of the way of the Kubernetes package manager, which it is
unrelated to.

It lists every `tmux` session across every host in your `~/.ssh/config`, tells you **which ones are
working and which are waiting on you**, says so out loud when one starts waiting, and opens any of
them in a terminal beside the list.

```
  ⎈  ╻ ╻┏━╸╻  ┏┳┓   │ [web-01/claude]
     ┣━┫┣╸ ┃  ┃┃┃    │
     ╹ ╹┗━╸┗━╸╹ ╹    │ ● Restructured the Jobs column header into two rows:
     2 waiting on you│ ✻ Cooked for 24m 14s
                     │ ❯ commit and push both
  web-01             │
  1  !  claude   ...  │
  2  o  shell    ...  │
  db-01              │
  3  *  claude   ...  │
                     │
  ?  4/4 hosts       │
```

Requires GTK4 and VTE. Nothing from pip.

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

Everything read off a screen is read **backwards** -- the last match, not the first. A terminal
scrolls, so the earliest "Cogitated for 26s", the earliest prompt and the earliest token count are
all things that happened several turns ago and are merely still visible.

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
| `ctrl-shift-c` / `ctrl-shift-v` | Copy / paste. Plain `ctrl-c` stays the interrupt |
| `ctrl-+` / `ctrl--` / `ctrl-0` | Text bigger, smaller, back to the terminal's own size |
| `ctrl-shift-a` | Select everything on the screen |
| `ctrl-click` a URL | Open it |
| right-click in a session | Copy, paste, and every link on the screen -- including ones tmux wrapped |
| hover a session | A bin appears: kill it, asked first -- there is no undo |
| `ctrl-shift-k` | Kill the selected session |
| right-click a row | The same, as a menu -- on a host too, for new session and its passwords |
| `+` | New session on the selected host, local or remote |
| server icon | Add a server to `~/.ssh/config` |
| `F1` or `?` | The guide: what the marks mean, every key, and who wrote it |

There is no title bar. GTK hides it fullscreen, so nothing that matters could live there anyway --
the actions are under the wordmark and the `x` is in the footer, both on screen whatever the window
is doing.

Every session carries the number that opens it, so the shortcut is never counted out. Past nine the
column is blank rather than promising a key that does not exist.

## It tells you

The panel is honest about who is waiting on you, but only to someone looking at it -- and the thing
worth knowing is exactly that a chat has been sitting there while you did something else. So it
says so: a desktop notification, once, and not again until the session has been something else in
between. Clicking it opens that session.

**Needs you** is announced the moment it happens -- nothing moves in that chat until you answer.
**An unsent draft is not**, because typing is a draft too: the box holds text from the first
keystroke, so announcing the state itself interrupted whoever was at the keyboard, about the
sentence they were in the middle of. A draft is announced once it has sat **unchanged for two
minutes**; every edit puts the clock back to the start.

States that were already true when helm started are on screen already, and are not announced.

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

## Replying

You type into the session. It is a real terminal on a real pty -- not a text box that builds a
command out of what you wrote -- so quotes, `$VAR`, backticks and semicolons are just characters,
and there is no layer left for them to be a command in.

The panel it grew out of could not do that: attaching took the whole screen, so it sent replies as
tmux buffers to avoid interpolating them into a command line. Opening a session next to the list
made the safest version of that feature the same thing as not having it. (`hosts.send_text` is in
the history if a reply-without-opening is ever wanted again.)

## Install

Requires Python 3.11+, GTK4 and VTE locally, and `tmux` on the hosts you connect to.

```bash
sudo pacman -S gtk4 vte4 python-gobject
mkdir -p ~/.local/share/helm/app
cp *.py ~/.local/share/helm/app/
cp bin/* ~/.local/bin/
helm --check      # probe every host once and print what it found
```

Nothing from pip, and no virtualenv: GTK, VTE and PyGObject are system packages, and a venv sealed
off from them cannot see GTK at all.

`cp org.omarchy.helm.desktop ~/.local/share/applications/` if you want it in a launcher. It is a
window now, so starting it through a TUI wrapper (`omarchy-launch-tui helm`, or a terminal binding) leaves an empty
terminal sitting beside the real one -- the terminal is hosting a process that no longer draws
anything in it.

Colours follow the [Omarchy](https://omarchy.org) desktop theme when present, and fall back to a
built-in palette otherwise. `HELM_THEME=light|dark` forces it.

## The font is the one you already chose

A session inside helm should be the size of the terminals beside it, so the font is not helm's to
pick: it is read out of the config of the terminal this machine actually has -- foot, ghostty,
alacritty or kitty, first one whose config is readable **and** whose binary is installed. Only if
none is does it fall back to `monospace 11`.

`HELM_FONT="JetBrainsMono Nerd Font 11"` overrides that outright.

`ctrl-+`, `ctrl--` and `ctrl-0` zoom, the way a terminal does -- a multiplier on that font rather
than a second opinion about it, applied to every session at once and remembered in
`~/.local/state/helm/zoom`. Worth knowing if it still looks off: your terminal may scale points by
the monitor's DPI where GTK uses the desktop's text-scaling factor, so the same "9" can land a
little different. That is what the zoom is for.

## One window

The list is a sidebar and the session opens beside it, in a real terminal widget -- VTE, the one
GNOME Terminal and Ptyxis are built on. Opening a chat costs you nothing you were already looking
at.

It used to throw a new window at the compositor per session, which is not a layout: on Hyprland
those landed on whichever workspace happened to be active, at whatever size dwindle felt like,
while the panel sat on another workspace entirely.

tmux is still doing the real work on the far side -- it is what makes a session survive the window
closing. You just stop seeing it as a window of its own. Each session names itself in tmux's status
bar (`[web-01/claude]`), which is how you know which one you are typing into.

helm itself has no title bar: GTK hides it when a window goes fullscreen, so nothing that matters
can live there.

## Licence

MIT.
