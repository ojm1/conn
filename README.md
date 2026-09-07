# conn

A window for the servers you keep coding-agent sessions on.
Reads **Claude Code** and **opencode**.

*You have the conn.*

Named for the bridge station: on a ship, the conn is the watch -- whoever has it is answerable for
where everything is and what it is doing. In *The Original Series* that console was the helm; by
*The Next Generation* the same station is Conn.

Installed as `conn` -- the command is `conn`. It was called helm until September 2026, and the
rename is exactly that collision: `helm` is the Kubernetes package manager, it is on the PATH of
everyone who would want this, and the two do entirely unrelated jobs.

It lists every `tmux` session across every host in your `~/.ssh/config`, tells you **which ones are
working and which are waiting on you**, says so out loud when one starts waiting, and opens any of
them in a terminal beside the list.

```
  ▌  ┏━╸┏━┓┏┓╻┏┓╻   │ [web-01/claude]
  ▌  ┃  ┃ ┃┃┃┃┃┃┃    │
  ▌  ┗━╸┗━┛╹┗┛╹┗┛    │ ● Restructured the Jobs column header into two rows:
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

Markers are matched **at the start of a line**, past whatever chrome the dialog draws in front of
them -- box bars, carets, indentation. A prompt Claude Code is asking begins its line; the same
words inside a sentence are someone *talking* about a prompt. Quote marks are excluded from that
chrome on purpose: prose puts one in front of the words every time, including at the start of a
wrapped line, and a dialog never does. (A session working on conn reporting itself blocked is how
this was found.)

Everything read off a screen is read **backwards** -- the last match, not the first. A terminal
scrolls, so the earliest "Cogitated for 26s", the earliest prompt and the earliest token count are
all things that happened several turns ago and are merely still visible.

A redesign on either side can break these markers. `tests/test_agent_state.py` runs the reader
against real captured screens — `python3 tests/test_agent_state.py`, no test dependencies — so a
break shows up as a failure rather than as a quietly wrong dashboard. `tests/test_hosts.py` does
the same for the transport: frame parsing, marker forgery, the mount table and `bin/ssh-mount`'s
guards, all against fakes — no display, no network. The window itself is checked by
`tests/test_gui.py`, which needs a display and a local tmux; it points the run at an ssh config
and state files of its own (`CONN_SSH_CONFIG` overrides where `hosts.py` reads `~/.ssh/config` —
ssh itself never sees it), so nothing of yours is probed or rewritten.

Adding a third agent means writing its patterns as a class in `agent_state.py`; the callers do not
change.

## Live, not polled

One held-open `ssh` channel per host runs a loop that re-dumps each session's screen and only sends
anything when it changed. Polling pays the network round trip on every check; this pays it once and
then streams. An idle host costs a heartbeat line every fifteen seconds -- which is also how the
panel tells a quiet connection from one that has silently died, and reconnects. A 45-second sweep
covers what the stream doesn't: uptime, disk, mounts, and the session list.

`CONN_NO_WATCH=1` falls back to plain polling.

The far side needs only `tmux` and a POSIX `sh`: the scripts are piped to `sh` explicitly rather
than handed to the login shell to parse, so a fish or tcsh user on the remote is fine.

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

`CONN_NO_LOCAL=1` hides the row. A `Host local` already in `~/.ssh/config` wins: an alias you can
really ssh to beats one the panel invented.

**If conn is itself running inside tmux**, a local session opens in its own window instead of taking
this one. It has to: tmux gives one terminal to one client, so attaching in place detaches the
session conn is running in -- conn disappears, and F12 then closes the window rather than bringing
it back, because the client it detaches is the outer one. Remote sessions are a different tmux
server and still take the terminal as before.

For the same reason the session conn is running in is left out of its own list: attaching to it is
the one attach that cannot go anywhere.

## In the window

```
  ▌  ┏━╸┏━┓┏┓╻┏┓╻        1  o  shell        idle
  ▌  ┃  ┃ ┃┃┃┃┃┃┃        2  !  claude       needs you
  ▌  ┗━╸┗━┛╹┗┛╹┗┛        3  *  shell        working
     2 waiting on you    ...
                         ?  6/7 hosts · 12 sessions
```

The bar beside the name is the condition of the whole fleet, and it is the one thing worth reading
from across the room: green is all clear, amber is something working, red is a session waiting on
you -- and grey is not knowing: a host dark, or a screen that cannot be read. A fleet out of sight
is not a fleet with nothing to do, so green is a claim the panel only makes once every host has
answered and every screen is recognised. It is the marks' own states, added up.

There is deliberately no ship's wheel. U+2388 is named HELM SYMBOL and reads as Kubernetes to
precisely the people who would run this.

The `?` at the foot of the list has the marks, the keys and who wrote it.

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
| `ctrl-shift-s` | Copy the whole screen -- no selection needed |
| `ctrl-+` / `ctrl--` / `ctrl-0` | Text bigger, smaller, back to the terminal's own size |
| `ctrl-shift-a` | Select everything on the screen |
| `ctrl-click` a URL | Open it |
| right-click in a session | Copy, paste, the whole screen, and every link on it -- including ones tmux wrapped |
| hover a session | A bin appears: kill it, asked first -- there is no undo |
| `ctrl-shift-k` | Kill the selected session |
| `ctrl-shift-r` | Rename it -- local or remote, nothing running in it is interrupted |
| right-click a row | Open, rename, kill -- and on a host, new session, files and its passwords |
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
between. Clicking it opens that session -- starting conn first if it has exited in the meantime,
which is what the D-Bus service file in the install section is for.

**Needs you** is announced the moment it happens -- nothing moves in that chat until you answer.
**An unsent draft is not**, because typing is a draft too: the box holds text from the first
keystroke, so announcing the state itself interrupted whoever was at the keyboard, about the
sentence they were in the middle of. A draft is announced once it has sat **unchanged for two
minutes**; every edit puts the clock back to the start.

States that were already true when conn started are on screen already, and are not announced.

### Selecting text an agent is sitting on

Claude Code and opencode both turn on mouse reporting, which means the drag never reaches the
terminal -- the agent gets it. **Hold shift while dragging** and VTE takes the mouse back; that
override is hardcoded in VTE and there is no setting that turns an application's mouse reporting
off. With nothing selected, copy is a no-op, so the footer says **nothing selected -- hold shift
while you drag** rather than leaving you to wonder whether the key is bound. A copy that works says
so too, with the number of characters, because the alternative is finding out at the other end.

`ctrl-shift-s` copies the **whole screen** and needs no selection at all -- conn already holds that
text, it is the same capture the state is read from.

## When it has been replaced underneath it

`cp *.py ~/.local/share/conn/app/` does not touch the window already running: it goes on running
the code it started with, and a feature added an hour ago is simply not there. So the footer says
**restart to update** once the files on disk are newer than the ones in memory, and clicking it
replaces the process with the installed one. The sessions are tmux and outlive it -- the views
close and come straight back.

## The one password

conn never sees it. Your ssh key is unlocked once -- by the desktop keyring at login -- and every
`ssh` it spawns rides the agent, which is why no host prompts you. If the agent is holding nothing,
the foot of the list says so and offers to run `ssh-add` in a terminal of its own: the passphrase
goes from your keyboard to `ssh-add`, never through conn.

Right-click a host for **passwords and keys** -- whatever else you keep for it: a database password,
an API key. They live in the desktop keyring, which the same login password already unlocks, so
"one password for all of it" is the arrangement that exists rather than a thing to build. conn
stores nothing itself: a value is fetched when you press Show, masked again on Hide, and a copy is
wiped off the clipboard after thirty seconds.

## First contact

The background probe never accepts a host key. It runs ssh with `StrictHostKeyChecking=yes`, so a
server whose key is not already in `known_hosts` shows as **down**, with *host key not verified* as
the reason under the pointer. Open a session on it once -- ssh itself shows the fingerprint and
asks -- and every probe after that rides the key you accepted. An unattended loop that trusted
whatever key the network offered would pin a man-in-the-middle before you had consciously connected
at all; the one trust decision stays yours. Worth knowing when you add a server: the row stays red
until that first real connect.

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
mkdir -p ~/.local/share/conn/app
cp *.py ~/.local/share/conn/app/
cp bin/* ~/.local/bin/
conn --check      # probe every host once and print what it found
```

Nothing from pip, and no virtualenv: GTK, VTE and PyGObject are system packages, and a venv sealed
off from them cannot see GTK at all.

For a launcher entry with its own icon:

```bash
cp org.omarchy.conn.desktop ~/.local/share/applications/
install -Dm644 icons/org.omarchy.conn.svg ~/.local/share/icons/hicolor/scalable/apps/org.omarchy.conn.svg
for s in 48 64 128 256; do
  rsvg-convert -w $s -h $s icons/org.omarchy.conn.svg \
    -o ~/.local/share/icons/hicolor/${s}x${s}/apps/org.omarchy.conn.png
done
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
```

The icon is named for the app id, so the window picks it up for the taskbar too. It is a
window now, so starting it through a TUI wrapper (`omarchy-launch-tui conn`, or a terminal binding) leaves an empty
terminal sitting beside the real one -- the terminal is hosting a process that no longer draws
anything in it.

And the D-Bus service file, which is what lets the session bus *start* conn: a notification
clicked after conn has exited, and `conn --notify` with no conn running, both land on the bus name
-- `DBusActivatable=true` in the desktop entry only names it. The `Exec` line has to be an
absolute path, because the bus does no PATH lookup, hence the sed:

```bash
mkdir -p ~/.local/share/dbus-1/services
sed "s|/home/you|$HOME|" org.omarchy.conn.service \
  > ~/.local/share/dbus-1/services/org.omarchy.conn.service
```

Colours follow the [Omarchy](https://omarchy.org) desktop theme when present, and fall back to a
built-in palette otherwise. `CONN_THEME=light|dark` forces it. Read once, at startup: a theme
changed while conn is running shows up on the next start -- the sessions are tmux, so restarting
costs nothing but the views.

## The font is the one you already chose

A session inside conn should be the size of the terminals beside it, so the font is not conn's to
pick: it is read out of the config of the terminal this machine actually has -- foot, ghostty,
alacritty or kitty, first one whose config is readable **and** whose binary is installed. Only if
none is does it fall back to `monospace 11`.

`CONN_FONT="JetBrainsMono Nerd Font 11"` overrides that outright.

`ctrl-+`, `ctrl--` and `ctrl-0` zoom, the way a terminal does -- a multiplier on that font rather
than a second opinion about it, applied to every session at once and remembered in
`~/.local/state/conn/zoom`. Worth knowing if it still looks off: your terminal may scale points by
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

conn itself has no title bar: GTK hides it when a window goes fullscreen, so nothing that matters
can live there.

## Licence

MIT.
