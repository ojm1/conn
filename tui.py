"""Server panel -- every host in ~/.ssh/config and what is running on it.

The panel refreshes itself in the background, so what you see is current
without you having to ask. Every action says what it did on the status line;
nothing here happens silently.
"""

from __future__ import annotations

import os
import subprocess
import queue
import threading
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (DataTable, Footer, Header, Input, Label, ListItem,
                             ListView, Static)

import agent_state
import hosts
from theming import load_palette, textual_theme

PALETTE = load_palette()

REFRESH_SECONDS = 45      # full sweep: uptime, disk, mounts, session list
WATCH_INTERVAL = 1.0      # how often the far side re-checks its screens
REDRAW_SECONDS = 0.4      # coalescing window for incoming frames
SESSIONS_WIDTH = 26       # before the session list is summarised
NEW_SESSION = "\x00new"   # sentinel from the session picker



def state_style(state: str) -> str:
    return {
        agent_state.WORKING: PALETTE.blue,
        agent_state.NEEDS_YOU: f"bold {PALETTE.red}",
        agent_state.DRAFT: f"bold {PALETTE.orange}",
        agent_state.READY: PALETTE.green,
        agent_state.SHELL: PALETTE.muted,
    }.get(state, PALETTE.muted)


def state_mark(state: str) -> str:
    return {
        agent_state.WORKING: "*",
        agent_state.NEEDS_YOU: "!",
        agent_state.DRAFT: "!",
        agent_state.READY: "o",
        agent_state.SHELL: ".",
    }.get(state, "?")


def ago(stamp: float) -> str:
    if not stamp:
        return "never"
    seconds = int(time.time() - stamp)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def tilde(path: str, user: str) -> str:
    """Shorten a remote home directory the way a shell prompt would."""
    for home in (f"/home/{user}", "/root" if user == "root" else None):
        if home and (path == home or path.startswith(home + "/")):
            return "~" + path[len(home):]
    return path


def running_in(session: dict) -> str:
    """What a session is actually sitting in, deduped across its panes."""
    commands = []
    for pane in session["panes"]:
        if pane["cmd"] not in commands:
            commands.append(pane["cmd"])
    return ", ".join(commands)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class Prompt(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", initial: str = ""):
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            yield Input(value=self.initial, placeholder=self.placeholder)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionPicker(ModalScreen[str | None]):
    """Choose which tmux session to attach to.

    Attaching blind is how you end up typing shell commands into a program
    somebody left running, so the panel shows what is there and what it is
    doing before it connects you to any of it.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, host: str, sessions: list[dict]):
        super().__init__()
        self.host = host
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Connect to {self.host}", id="dialog-title")
            items = []
            for index, session in enumerate(self.sessions):
                line = Text()
                line.append("  ")
                line.append("*" if session["attached"] else " ",
                            style=PALETTE.green)
                line.append(f" {session['name']:<14}", style="bold")
                busy = running_in(session)
                if busy and busy not in ("bash", "zsh", "sh", "fish"):
                    line.append(f"running {busy}", style=PALETTE.orange)
                else:
                    line.append("shell", style=PALETTE.muted)
                items.append(ListItem(Label(line), id=f"pick-{index}"))

            new = Text("   + new session", style=PALETTE.accent)
            items.append(ListItem(Label(new), id="pick-new"))
            yield ListView(*items, id="session-list")
            yield Label("enter attach    esc cancel", id="dialog-keys")

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id == "pick-new":
            self.dismiss(NEW_SESSION)
            return
        index = int(item_id.removeprefix("pick-"))
        self.dismiss(self.sessions[index]["name"])

    def action_cancel(self) -> None:
        self.dismiss(None)


class Help(ModalScreen[None]):
    """The key reference, one keystroke away.

    Grouped by what you are trying to do rather than alphabetically -- the
    question is always "how do I answer this chat", never "what does j do".
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("f1", "close", "Close"),
    ]

    SECTIONS = [
        ("Answering a chat", [
            ("enter", "reply to the selected chat, right here"),
            ("enter", "empty box: submits the draft already sitting there"),
            ("esc", "leave the reply box"),
        ]),
        ("Going into one", [
            ("o", "open the chat in this window -- nothing new is spawned"),
            ("w", "open it in its OWN window, tiled beside the panel"),
            ("W", "open every chat that needs you, one window each"),
            ("F12", "leave it and come back here (ctrl-b d also works)"),
        ]),
        ("Moving around", [
            ("1-9", "select that chat"),
            ("up/down", "move the selection"),
            ("s", "show/hide the sidebar"),
            ("v", "switch the list between chats and hosts"),
            ("/", "filter the list, esc clears it"),
        ]),
        ("Hosts", [
            ("n", "new 'shell' session on the selected host"),
            ("r", "refresh everything now"),
            ("f / u", "mount / unmount the host at ~/mnt/<host>"),
            ("a", "add a server to ~/.ssh/config"),
            ("k", "install your key there (ssh-copy-id)"),
            ("e", "edit ~/.ssh/config, reloads on exit"),
        ]),
    ]

    STATES = [
        (agent_state.WORKING, "busy, or running background agents -- leave it"),
        (agent_state.NEEDS_YOU, "blocked on a prompt; nothing moves until you answer"),
        (agent_state.DRAFT, "text left in the box, never sent -- looks done, isn't"),
        (agent_state.READY, "empty prompt, waiting for you"),
        (agent_state.SHELL, "not an agent session -- a plain shell"),
        (agent_state.UNKNOWN, "could not read the screen -- open it and look"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("Server panel", id="dialog-title")
            with VerticalScroll(id="help-body"):
                yield Static(self.render_help())
            yield Label("esc or ? closes", id="dialog-keys")

    def render_help(self) -> Text:
        # The marks come first: "is it working or does it want me" is the
        # question the panel exists to answer, so it should not be scrolled to.
        text = Text(no_wrap=True, overflow="ellipsis")

        text.append("What the marks mean\n", style=PALETTE.heading)
        for state, what in self.STATES:
            label = agent_state.LABELS.get(state, state)
            text.append(f"  {state_mark(state):<3}", style=state_style(state))
            text.append(f"{label:<14}", style=state_style(state))
            text.append(f"{what}\n")
        text.append("\n")

        for heading, rows in self.SECTIONS:
            text.append(f"{heading}\n", style=PALETTE.heading)
            for key, what in rows:
                text.append(f"  {key:<10}", style=f"bold {PALETTE.accent}")
                text.append(f"{what}\n")
            text.append("\n")

        text.append("Worth knowing\n", style=PALETTE.heading)
        for line in [
            "The list refreshes itself every 45s -- you rarely need r.",
            "The main pane is the chat's real screen, read live.",
            "A toast fires when a chat newly needs you, once per change.",
            "State is read off the screen, so 'unknown' means look yourself.",
        ]:
            text.append(f"  {line}\n", style=PALETTE.muted)
        return text

    def action_close(self) -> None:
        self.dismiss(None)


class AddHost(ModalScreen[dict | None]):
    """Add a Host block without hand-editing the config."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    FIELDS = [
        ("name", "short name you'll type, e.g. staging"),
        ("hostname", "hostname or IP, e.g. 203.0.113.10"),
        ("user", "login user, e.g. root"),
        ("port", "22"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add a server", id="dialog-title")
            for field, placeholder in self.FIELDS:
                yield Label(field, classes="field-label")
                yield Input(placeholder=placeholder, id=f"in-{field}")
            yield Label("enter next field    ctrl+s save    esc cancel",
                        id="dialog-keys")

    def on_mount(self) -> None:
        self.query_one("#in-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        index = inputs.index(event.input)
        if index + 1 < len(inputs):
            inputs[index + 1].focus()
        else:
            self.action_save()

    def action_save(self) -> None:
        values = {field: self.query_one(f"#in-{field}", Input).value
                  for field, _ in self.FIELDS}
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

class HostTable(DataTable):
    """DataTable claims enter for itself, so the App-level Connect binding
    never reaches the footer. Re-declaring it here puts the hint back."""

    BINDINGS = [Binding("enter", "select_cursor", "Reply")]


class SSHPanel(App):
    TITLE = "helm"
    CSS = """
    Screen { layers: base overlay; }

    #body { height: 1fr; }

    #sidebar {
        width: 34;
        border-right: solid $panel-lighten-2;
        padding: 0 1;
    }
    #sidebar.hidden { display: none; }
    #hosts { height: 1fr; }

    #main { width: 1fr; padding: 0 1; }
    #detail-body { height: 1fr; }
    #hint { height: 1; color: $text-muted; }
    #reply { border: tall $panel-lighten-2; }
    #reply:focus { border: tall $accent; }
    #reply.hidden { display: none; }

    #filter { display: none; }
    #filter.visible { display: block; }

    #status { height: 1; padding: 0 1; color: $text-muted; }

    #dialog {
        width: 62;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }
    #dialog-title { padding-bottom: 1; text-style: bold; color: $accent; }
    #dialog-keys { padding-top: 1; color: $text-muted; }
    .field-label { color: $text-muted; }

    #dialog.wide { width: 84; }
    #help-body { height: auto; max-height: 24; }
    #session-list { height: auto; max-height: 12; background: transparent; }
    #session-list ListItem { padding: 0 1; background: transparent; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
        Binding("f1", "help", "Help", show=False),
        Binding("v", "switch_view", "Chats/Hosts"),
        Binding("enter", "reply", "Reply"),
        Binding("o", "connect", "Open"),
        Binding("w", "open_window", "New window"),
        Binding("W", "open_needy", "Open all needing you"),
        Binding("s", "toggle_sidebar", "Sidebar"),
        Binding("n", "shell", "New shell"),
        Binding("r", "refresh_all", "Refresh"),
        Binding("f", "files", "Files"),
        Binding("u", "unmount", "Unmount"),
        Binding("a", "add_host", "Add"),
        Binding("k", "copy_key", "Install key"),
        Binding("e", "edit_config", "Edit config"),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "", show=False),
    ] + [
        # Number keys jump straight into a chat.
        Binding(str(n + 1), f"jump({n})", "", show=False) for n in range(9)
    ]

    def __init__(self):
        super().__init__()
        self.rows: dict[str, dict] = {}
        self.order: list[str] = []
        self.listed: list[str] = []
        self.inflight: set[str] = set()
        self.query_text = ""
        self.sidebar_hidden = False
        self.detail_pinned = False   # set once the user toggles it themselves
        self.view = "chats"          # chats | hosts
        self.chat_rows: list[dict] = []
        self.watchers: dict[str, object] = {}
        self.watched: set[str] = set()
        self.stopping = False
        self.live: set[str] = set()
        self.dirty: set[str] = set()
        self.frames: queue.Queue = queue.Queue(maxsize=200)
        self.seen_states: dict[str, str] = {}

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield HostTable(id="hosts", cursor_type="row",
                                zebra_stripes=False)
                yield Input(placeholder="filter...", id="filter")
            with Vertical(id="main"):
                with VerticalScroll(id="screen-scroll"):
                    yield Static("", id="detail-body")
                yield Static("", id="hint")
                yield Input(placeholder="reply...", id="reply")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.theme_obj = textual_theme(PALETTE)
        self.register_theme(self.theme_obj)
        self.theme = "omarchy"

        self.setup_columns()
        self.query_one("#hosts", HostTable).focus()

        self.load_hosts()
        self.set_interval(REFRESH_SECONDS, self.sweep)
        self.set_interval(REDRAW_SECONDS, self.flush_frames)
        self.apply_layout(self.size.width)
        self.set_interval(1.0, self.tick)

    # -- data -------------------------------------------------------------

    def load_hosts(self) -> None:
        """Re-read the config, keeping whatever we already know about a host
        that is still there."""
        self.order = hosts.list_hosts()
        for host in self.order:
            self.rows.setdefault(host, hosts.blank(host))
        for gone in [h for h in self.rows if h not in self.order]:
            del self.rows[gone]

        self.render_table()
        # HELM_NO_WATCH=1 falls back to plain 45s polling, which is also
        # the escape hatch if streaming ever misbehaves on a given box.
        if os.environ.get("HELM_NO_WATCH") != "1":
            for host in self.order:
                if host not in self.watched:
                    self.watched.add(host)
                    self.watch_host(host)
        if not self.order:
            self.status("No servers in ~/.ssh/config yet -- press a to add one.")
        else:
            self.sweep()

    def sweep(self) -> None:
        started = [h for h in self.order if self.start_probe(h)]
        if started:
            count = len(started)
            self.status(f"checking {count} host{'s' if count != 1 else ''}...")

    def start_probe(self, host: str) -> bool:
        """Claim the host on the main thread so two sweeps cannot both
        launch a probe for it. Returns whether this call started one."""
        if host in self.inflight:
            return False
        self.inflight.add(host)
        self.render_table()
        self.probe_host(host)
        return True

    def watch_host(self, host: str) -> None:
        """Start the stream for one host on a daemon thread.

        Deliberately NOT a Textual worker. Workers are non-daemon threads and
        Textual waits for them on the way out, so a stream that loops until
        told to stop makes the app impossible to quit -- pressing q did
        nothing at all. A daemon thread never holds up interpreter exit.
        """
        thread = threading.Thread(target=self._stream, args=(host,),
                                  name=f"watch-{host}", daemon=True)
        thread.start()

    def _stream(self, host: str) -> None:
        """Hold one ssh channel open for a host and read screen frames off it.

        Polling would pay the network round trip on every single
        check. This pays it once. The far side only speaks when a screen
        actually changed, so an idle host costs nothing at all.
        """
        while not self.stopping:
            try:
                proc = hosts.watch_screens(host, WATCH_INTERVAL)
            except OSError:
                return
            self.watchers[host] = proc
            buffer: list[str] = []
            try:
                # readline(), not "for line in proc.stdout": iterating a pipe
                # uses a read-ahead buffer that sits on frames until enough
                # bytes pile up, which stalls a live view indefinitely.
                while not self.stopping:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if line.startswith("###FRAME"):
                        frame = hosts.parse_frame(buffer)
                        buffer = []
                        # A queue, not call_from_thread: that call blocks the
                        # streaming thread until the event loop runs it, and
                        # with several streams it starves input -- keys stop
                        # registering. The UI timer drains this instead.
                        try:
                            self.frames.put_nowait((host, frame))
                        except queue.Full:
                            pass
                    else:
                        buffer.append(line.rstrip("\n"))
            except (OSError, ValueError):
                pass
            finally:
                self.watchers.pop(host, None)
                try:
                    proc.terminate()
                except Exception:
                    pass
                self.live.discard(host)

            if self.stopping:
                self.watched.discard(host)
                return
            time.sleep(3)      # link dropped -- back off, then reconnect

    def absorb_frame(self, host: str, frame: dict) -> None:
        row = self.rows.get(host)
        if row is None:
            return
        self.live.add(host)

        known = {session["name"] for session in row["sessions"]}
        if set(frame) != known:
            # A session appeared or went away: the stream only carries screens,
            # so the list itself needs a real probe.
            self.start_probe(host)
            return

        for session in row["sessions"]:
            data = frame.get(session["name"])
            if not data:
                continue
            session["screen"] = data["screen"]
            session["agent"] = agent_state.classify(
                data["screen"], data["commands"])

        # Rendering happens on a timer instead. A busy host emits several
        # frames a second, and rebuilding the table on each one starves
        # Textual's input loop -- the symptom is keys quietly doing nothing.
        self.dirty.add(host)

    def flush_frames(self) -> None:
        """Drain whatever the streams queued, then redraw once."""
        while True:
            try:
                host, frame = self.frames.get_nowait()
            except queue.Empty:
                break
            self.absorb_frame(host, frame)

        if not self.dirty:
            return
        changed = list(self.dirty)
        self.dirty.clear()
        for host in changed:
            row = self.rows.get(host)
            if row:
                self.alert_on_changes(row)
        self.render_table()
        self.render_detail()
        self.status(self.summary())

    def shutdown_watchers(self) -> None:
        """Stop the streams and let their threads finish.

        This has to happen *before* exit, not during unmount: Textual waits for
        worker threads on the way out, and a watcher loops until told to stop,
        so leaving it to on_unmount deadlocks the quit -- the app simply
        refuses to close.
        """
        self.stopping = True
        for proc in list(self.watchers.values()):
            try:
                proc.terminate()
            except Exception:
                pass
        self.watchers.clear()

    def on_unmount(self) -> None:
        self.shutdown_watchers()

    @work(thread=True)
    def probe_host(self, host: str) -> None:
        try:
            row = hosts.probe(host)
        finally:
            self.inflight.discard(host)
        self.call_from_thread(self.absorb, row)

    def absorb(self, row: dict) -> None:
        # The probe carries its own screen capture, taken when the request went
        # out -- which is older than whatever the stream has already delivered.
        # Letting it win makes the main pane jump backwards in time every 45
        # seconds. The probe is authoritative for the session *list*, the
        # stream is authoritative for what is on those screens.
        host = row["host"]
        if host in self.live:
            previous = {session["name"]: session
                        for session in self.rows.get(host, {}).get("sessions", [])}
            for session in row["sessions"]:
                older = previous.get(session["name"])
                if older and older.get("screen"):
                    session["screen"] = older["screen"]
                    session["agent"] = older["agent"]

        self.rows[host] = row
        self.alert_on_changes(row)
        self.render_table()
        self.render_detail()
        if not self.inflight:
            self.status(self.summary())

    def alert_on_changes(self, row: dict) -> None:
        """Raise a toast when a chat newly needs a human.

        Only on the transition -- a chat that has been waiting for an hour
        should not re-announce itself every 45 seconds.
        """
        for session in row["sessions"]:
            key = f"{row['host']}/{session['name']}"
            state = session["agent"]["state"]
            was = self.seen_states.get(key)
            self.seen_states[key] = state
            if was is None or state == was:
                continue
            if agent_state.needs_attention(state):
                self.notify(session["agent"]["detail"][:70] or "waiting for you",
                            title=f"{key} needs you",
                            severity="warning", timeout=12)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for row in self.rows.values():
            for session in row["sessions"]:
                state = session["agent"]["state"]
                counts[state] = counts.get(state, 0) + 1

        order = [(agent_state.NEEDS_YOU, "need you"),
                 (agent_state.DRAFT, "unsent"),
                 (agent_state.WORKING, "working"),
                 (agent_state.READY, "idle"),
                 (agent_state.SHELL, "shell")]
        parts = [f"{counts[state]} {word}" for state, word in order if counts.get(state)]

        down = [h for h, r in self.rows.items() if r["state"] == "down"]
        if down:
            parts.append(f"{len(down)} host{'s' if len(down) != 1 else ''} down")
        if not parts:
            parts = ["no sessions"]
        return "  -  ".join(parts) + f"   ({time.strftime('%H:%M:%S')})"

    def tick(self) -> None:
        """Keep the 'checked Ns ago' line honest between sweeps."""
        if not self.inflight and not self.dirty:
            self.render_detail()

    # -- rendering --------------------------------------------------------

    def dot(self, row: dict) -> Text:
        if row["host"] in self.inflight:
            return Text("*", style=PALETTE.accent)
        return {
            "up": Text("*", style=PALETTE.green),
            "down": Text("x", style=PALETTE.red),
            "nokey": Text("!", style=PALETTE.yellow),
        }.get(row["state"], Text("?", style=PALETTE.muted))

    def sessions_cell(self, row: dict) -> Text:
        if row["host"] in self.inflight and row["state"] == "unknown":
            return Text("checking...", style=PALETTE.muted)
        if row["state"] == "down":
            return Text("offline", style=PALETTE.red)
        if row["state"] == "nokey":
            return Text("key not installed", style=PALETTE.yellow)
        if row["state"] == "unknown":
            return Text("not checked", style=PALETTE.muted)
        if not row["sessions"]:
            return Text("idle", style=PALETTE.muted)

        text = Text()
        width = 0
        for index, session in enumerate(row["sessions"]):
            # A box with a dozen sessions must not push the rest of the row
            # off screen, so the list stops and says how many are left.
            if width and width + len(session["name"]) > SESSIONS_WIDTH:
                left = len(row["sessions"]) - index
                text.append(f", +{left} more", style=PALETTE.muted)
                break
            if index:
                text.append(", ", style=PALETTE.muted)
                width += 2
            busy = running_in(session)
            style = (PALETTE.orange
                     if busy not in ("bash", "zsh", "sh", "fish", "")
                     else PALETTE.foreground)
            text.append(session["name"], style=style)
            width += len(session["name"])
        return text

    def setup_columns(self) -> None:
        table = self.query_one("#hosts", HostTable)
        table.clear(columns=True)
        # State text and detail live in the main pane; the sidebar carries
        # only what identifies a row, so it stays readable at 34 columns.
        if self.view == "chats":
            table.add_column("", width=2)      # 1..9 shortcut
            table.add_column("", width=1)      # state mark
            table.add_column("chat")
        else:
            table.add_column("", width=1)
            table.add_column("host")

    def chats(self) -> list[dict]:
        """Every session on every host, flattened into one list.

        This is the view that matters day to day: the question is never "which
        box" but "which chat needs me", and those two are not the same thing.
        """
        found = []
        for host in self.order:
            row = self.rows[host]
            for session in row["sessions"]:
                label = f"{host}/{session['name']}"
                if self.query_text.lower() not in label.lower():
                    continue
                found.append({"host": host, "session": session, "label": label})
        return found

    def render_table(self) -> None:
        table = self.query_one("#hosts", HostTable)
        previous = table.cursor_row
        table.clear()

        if self.view == "chats":
            self.chat_rows = self.chats()
            self.listed = [c["host"] for c in self.chat_rows]
            for index, chat in enumerate(self.chat_rows):
                info = chat["session"]["agent"]
                shortcut = str(index + 1) if index < 9 else ""
                table.add_row(
                    Text(shortcut, style=PALETTE.muted),
                    Text(state_mark(info["state"]), style=state_style(info["state"])),
                    Text(chat["label"][:26], style=state_style(info["state"])))
            if not self.chat_rows:
                self.listed = []
        else:
            self.listed = [h for h in self.order
                           if self.query_text.lower() in h.lower()]
            for host in self.listed:
                row = self.rows[host]
                mount_mark = " +" if row["mounted"] else ""
                table.add_row(
                    self.dot(row),
                    Text((host + mount_mark)[:28], style="bold"))

        count = len(self.chat_rows) if self.view == "chats" else len(self.listed)
        if count:
            table.move_cursor(row=min(previous, count - 1))

    def selected(self) -> dict | None:
        """The host row under the cursor, in either view."""
        table = self.query_one("#hosts", HostTable)
        if table.cursor_row is None:
            return None
        if self.view == "chats":
            if table.cursor_row >= len(self.chat_rows):
                return None
            return self.rows[self.chat_rows[table.cursor_row]["host"]]
        if not self.listed or table.cursor_row >= len(self.listed):
            return None
        return self.rows[self.listed[table.cursor_row]]

    def selected_chat(self) -> dict | None:
        table = self.query_one("#hosts", HostTable)
        if self.view != "chats" or table.cursor_row is None:
            return None
        if table.cursor_row >= len(self.chat_rows):
            return None
        return self.chat_rows[table.cursor_row]

    def render_detail(self) -> None:
        if self.view == "chats":
            self.render_chat_detail()
            return
        body = self.query_one("#detail-body", Static)
        self.hint("enter  open a session here      v  back to chats")
        self.query_one("#reply", Input).add_class("hidden")
        row = self.selected()
        if row is None:
            if self.query_text and self.order:
                body.update(Text(f"No host matches '{self.query_text}'.\n"
                                 "esc clears the filter.", style=PALETTE.muted))
            elif not self.order:
                body.update(Text("No servers yet.\n\nPress a to add one.",
                                 style=PALETTE.muted))
            else:
                body.update("")
            return

        text = Text()
        text.append(f"{row['host']}\n", style=f"bold {PALETTE.accent}")
        text.append(f"{row['target']}\n\n", style=PALETTE.muted)

        if row["state"] == "nokey":
            text.append("Key not installed.\n", style=f"bold {PALETTE.yellow}")
            text.append("The box answers but refuses your key.\n\n",
                        style=PALETTE.muted)
            text.append("Press k", style=f"bold {PALETTE.accent}")
            text.append(" to run ssh-copy-id.\n", style=PALETTE.muted)
        elif row["state"] == "down":
            text.append("Unreachable.\n", style=f"bold {PALETTE.red}")
            if row["error"]:
                text.append(f"{row['error']}\n", style=PALETTE.muted)
        elif row["state"] == "up":
            for label, value in (("up", row["uptime"]),
                                 ("load", f"{row['load']}  ({row['cpus']} cpu)"
                                  if row["cpus"] else row["load"]),
                                 ("mem", row["mem"]),
                                 ("disk", row["disk"])):
                if value.strip():
                    text.append(f"{label:<5}", style=PALETTE.muted)
                    text.append(f"{value}\n")
            text.append("\n")

            if row["sessions"]:
                text.append("TMUX SESSIONS\n", style=PALETTE.heading)
                for session in row["sessions"]:
                    text.append(" *" if session["attached"] else "  ",
                                style=PALETTE.green)
                    text.append(f" {session['name']}", style="bold")
                    plural = "" if session["windows"] == "1" else "s"
                    text.append(f"  {session['windows']} window{plural}"
                                f"{', attached' if session['attached'] else ''}\n",
                                style=PALETTE.muted)
                    user = row["target"].partition("@")[0]
                    for pane in session["panes"]:
                        text.append(f"      {pane['cmd']:<12}", style=PALETTE.orange)
                        text.append(f"{tilde(pane['path'], user)}\n",
                                    style=PALETTE.muted)
            else:
                text.append("no tmux sessions\n", style=PALETTE.muted)
        else:
            text.append("not checked yet\n", style=PALETTE.muted)

        text.append("\n")
        if row["mounted"]:
            text.append("files  ", style=PALETTE.muted)
            text.append(f"~/mnt/{row['host']}\n", style=PALETTE.green)
        text.append(f"checked {ago(row['checked'])}\n", style=PALETTE.muted)
        body.update(text)

    def detail_width(self) -> int:
        """Usable columns in the detail pane.

        Measured rather than assumed: a hardcoded width wraps by a character or
        two once padding and the border are counted, and a wrapped screen
        preview is unreadable.
        """
        try:
            width = self.query_one("#detail-body", Static).size.width
        except Exception:
            width = 0
        return max(24, (width or 46) - 1)

    def screen_lines(self) -> int:
        """How many lines of remote screen the pane can actually show.

        Measured, not guessed. A fixed number wastes most of a tall window --
        the whole point of the main pane is to show as much of the chat as
        there is room for.
        """
        try:
            height = self.query_one("#screen-scroll").size.height
        except Exception:
            height = 0
        # the label / state / attached / blank / SCREEN heading sit above it
        return max(6, (height or 24) - 6)

    def render_chat_detail(self) -> None:
        """Show the chat's own screen, so you can read what it is saying
        without attaching and without disturbing it."""
        body = self.query_one("#detail-body", Static)
        chat = self.selected_chat()
        if chat is None:
            self.hint("")
            if self.query_text:
                body.update(Text(f"No chat matches '{self.query_text}'.",
                                 style=PALETTE.muted))
            else:
                body.update(Text("No sessions running.\n\n"
                                 "v switches to the host list;\n"
                                 "n starts a shell on the selected host.",
                                 style=PALETTE.muted))
            return

        session = chat["session"]
        info = session["agent"]

        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(f"{chat['label'][:self.detail_width()]}\n",
                    style=f"bold {PALETTE.accent}")
        text.append(info["label"], style=state_style(info["state"]))
        if info["detail"]:
            room = max(12, self.detail_width() - len(info["label"]) - 2)
            text.append(f"  {info['detail'][:room]}", style=PALETTE.muted)
        text.append("\n")
        if info["tokens"]:
            text.append(f"context {info['tokens']} tokens\n", style=PALETTE.muted)
        # Worth saying out loud once a host runs more than one kind of agent:
        # "idle" means different things in different chrome.
        if info.get("agent"):
            text.append(f"{info['agent']}\n", style=PALETTE.muted)
        text.append(f"{'attached' if session['attached'] else 'detached'}"
                    f"  -  {ago(session.get('activity', 0))}  -  ",
                    style=PALETTE.muted)
        if chat["host"] in self.live:
            text.append("live\n\n", style=PALETTE.green)
        else:
            text.append("snapshot\n\n", style=PALETTE.muted)

        self.hint("enter  reply below    o  open it here    "
                  "w  open in a new window    F12  come back")
        self.query_one("#reply", Input).remove_class("hidden")

        screen = session.get("screen", "")
        if screen:
            width = self.detail_width()
            text.append("SCREEN\n", style=PALETTE.heading)
            # Collapse runs of blank lines -- agent output is airy, and
            # the preview is only ~18 lines tall.
            lines = []
            for line in screen.splitlines():
                if not line.strip() and (not lines or not lines[-1].strip()):
                    continue
                lines.append(line)
            for line in lines[-self.screen_lines():]:
                text.append(line[:width] + "\n", style=PALETTE.muted)
        body.update(text)

    def on_data_table_row_highlighted(self, event) -> None:
        self.render_detail()

    def on_data_table_row_selected(self, event) -> None:
        if self.view == "chats":
            self.action_reply()
        else:
            self.action_connect()

    def status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def hint(self, message: str) -> None:
        self.query_one("#hint", Static).update(
            Text(message, style=PALETTE.muted))

    # -- actions ----------------------------------------------------------

    def attach(self, host: str, session: str) -> None:
        """Hand this terminal over to the remote session, then take it back.

        Suspending rather than spawning a window is what keeps everything in
        one place: the remote app gets a real terminal at full speed with no
        emulation in between, and detaching (ctrl-b d) drops you straight back
        into the panel.
        """
        with self.suspend():
            subprocess.run(["ssh-connect", host, session])
        self.start_probe(host)
        self.status(f"back from {host}/{session}")
        self.refresh(layout=True)

    def action_connect(self) -> None:
        if self.view == "chats":
            chat = self.selected_chat()
            if chat is None:
                return
            self.attach(chat["host"], chat["session"]["name"])
            return

        row = self.selected()
        if row is None:
            return
        if row["state"] == "nokey":
            self.status(f"{row['host']}: key not installed -- press k first")
            return
        if not row["sessions"]:
            self.attach(row["host"], "shell")
            return

        def chosen(name: str | None) -> None:
            if name is None:
                return
            if name == NEW_SESSION:
                self.prompt_new_session(row["host"])
                return
            self.attach(row["host"], name)

        self.push_screen(SessionPicker(row["host"], row["sessions"]), chosen)

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_open_window(self) -> None:
        """Open the selected chat in its own terminal window.

        `o` hands this window over to the session, which is right when you want
        one thing. This is for the other case: panel on one side, several live
        sessions tiled beside it. The window manager does the tiling; all this
        does is launch it and stay put.
        """
        if self.view == "chats":
            chat = self.selected_chat()
            if chat is None:
                return
            host, session = chat["host"], chat["session"]["name"]
        else:
            row = self.selected()
            if row is None:
                return
            host = row["host"]
            session = row["sessions"][0]["name"] if row["sessions"] else "shell"

        self.spawn_windows([(host, session)])
        self.status(f"{host}/{session} opening in a new window")

    def needy(self) -> list[dict]:
        """Chats blocked on a human: a permission prompt, or an unsent draft."""
        return [chat for chat in self.chats()
                if agent_state.needs_attention(chat["session"]["agent"]["state"])]

    def action_open_needy(self) -> None:
        """Open every chat that is waiting on you, each in its own window."""
        targets = self.needy()
        if not targets:
            self.status("nothing is waiting on you")
            return
        self.status(f"opening {len(targets)} chat"
                    f"{'s' if len(targets) != 1 else ''} that need you...")
        self.spawn_windows([(c["host"], c["session"]["name"]) for c in targets])

    @work(thread=True)
    def spawn_windows(self, targets: list) -> None:
        """Open session windows without the panel being carved up.

        Each new window splits whichever window has focus, and the split runs
        along that window's longer axis. So a full-width panel splits
        left/right (panel left, session right) and each session after that
        stacks beside the previous one.
        """
        for host, session in targets:
            hosts.launch(["ssh-connect", host, session])
            # let the compositor map and place each window before the next, or
            # they race and land on top of each other
            time.sleep(0.7)

    def action_switch_view(self) -> None:
        self.view = "hosts" if self.view == "chats" else "chats"
        self.setup_columns()
        self.render_table()
        self.render_detail()
        self.status(f"{self.view} view")

    def action_jump(self, index: int) -> None:
        """Number keys select a chat. Selecting is enough now that you can read
        it and reply to it without ever leaving the panel."""
        if self.view != "chats" or index >= len(self.chat_rows):
            return
        table = self.query_one("#hosts", HostTable)
        table.move_cursor(row=index)
        self.render_detail()

    # -- replying without attaching ---------------------------------------

    def action_reply(self) -> None:
        chat = self.selected_chat()
        if chat is None:
            if self.view != "chats":
                self.action_connect()
            return
        box = self.query_one("#reply", Input)
        box.placeholder = f"reply to {chat['label']}..."
        box.focus()

    def send_reply(self, text: str) -> None:
        chat = self.selected_chat()
        if chat is None:
            return
        host, session = chat["host"], chat["session"]["name"]
        info = chat["session"]["agent"]

        # An empty box submits whatever is already sitting unsent in the chat,
        # which is the exact fix for a draft you left behind.
        if not text.strip():
            if info["state"] != agent_state.DRAFT:
                self.status("nothing to send -- type a reply first")
                return
            self.status(f"submitting the draft in {chat['label']}...")
        else:
            self.status(f"sending to {chat['label']}...")

        self.deliver(host, session, text)

    @work(thread=True)
    def deliver(self, host: str, session: str, text: str) -> None:
        try:
            hosts.send_text(host, session, text)
        except (hosts.HostError, OSError) as exc:
            self.call_from_thread(self.status, f"{host}/{session}: {exc}")
            self.call_from_thread(self.notify, str(exc),
                                  title="Could not send", severity="error")
            return
        self.call_from_thread(self.status, f"sent to {host}/{session}")
        self.call_from_thread(self.start_probe, host)

    def prompt_new_session(self, host: str) -> None:
        def named(name: str | None) -> None:
            if not name:
                return
            self.attach(host, name)
        self.push_screen(Prompt("New session name", "shell", "shell"), named)

    def action_shell(self) -> None:
        row = self.selected()
        if row is None:
            return
        self.attach(row["host"], "shell")

    def action_refresh_all(self) -> None:
        global PALETTE
        fresh = load_palette()
        if fresh.signature() != PALETTE.signature():
            PALETTE = fresh
            self.register_theme(textual_theme(fresh))
            self.theme = "omarchy"
        self.load_hosts()
        self.status("refreshing all hosts...")

    def mount_state(self, row: dict) -> bool:
        """Ask the filesystem, not the cached probe.

        The probe only refreshes every 45s, so right after pressing u the
        cached flag still says "mounted" -- and f would then try to open a
        directory that no longer exists instead of remounting it. os.path
        .ismount is a local call, so there is no reason to trust a stale copy.
        """
        mounted = hosts.is_mounted(row["host"])
        row["mounted"] = mounted
        return mounted

    def action_files(self) -> None:
        row = self.selected()
        if row is None:
            return
        if self.mount_state(row):
            hosts.open_files(f"{hosts.MNT_ROOT / row['host']}")
            self.status(f"{row['host']}: opening ~/mnt/{row['host']}")
            return
        self.status(f"{row['host']}: mounting...")
        self.do_mount(row["host"])

    @work(thread=True)
    def do_mount(self, host: str) -> None:
        try:
            path = hosts.mount(host)
        except (hosts.HostError, OSError) as exc:
            self.call_from_thread(self.status, f"{host}: mount failed -- {exc}")
            self.call_from_thread(self.notify, str(exc),
                                  title=f"{host}: mount failed", severity="error")
            return
        hosts.open_files(path)
        row = self.rows.get(host)
        if row is not None:
            row["mounted"] = True
        self.call_from_thread(self.status, f"{host}: mounted at {path}")
        self.call_from_thread(self.notify, path, title=f"{host} mounted")
        self.call_from_thread(self.start_probe, host)

    def action_unmount(self) -> None:
        row = self.selected()
        if row is None:
            return
        if not self.mount_state(row):
            self.status(f"{row['host']}: not mounted")
            return
        self.status(f"{row['host']}: unmounting...")
        self.do_unmount(row["host"])

    @work(thread=True)
    def do_unmount(self, host: str) -> None:
        # On a worker: fusermount can block for seconds on a busy mount, and
        # doing that on the main thread freezes the whole panel.
        try:
            hosts.unmount(host)
        except (hosts.HostError, OSError) as exc:
            self.call_from_thread(self.status, f"{host}: {exc}")
            self.call_from_thread(self.notify, str(exc),
                                  title=f"{host}: unmount failed", severity="error")
            return
        self.call_from_thread(self.status, f"{host}: unmounted")
        self.call_from_thread(self.start_probe, host)

    def action_copy_key(self) -> None:
        row = self.selected()
        if row is None:
            return
        hosts.copy_key(row["host"])
        self.status(f"{row['host']}: ssh-copy-id running in a new window "
                    "-- press r when it finishes")

    def action_add_host(self) -> None:
        def added(values: dict | None) -> None:
            if not values:
                return
            try:
                backup = hosts.add_host(values["name"], values["hostname"],
                                        values["user"], values["port"] or "22")
            except (hosts.HostError, OSError) as exc:
                self.status(str(exc))
                self.notify(str(exc), title="Could not add host", severity="error")
                return
            self.load_hosts()
            self.status(f"added {values['name']}  -  backup at {backup.name}"
                        "  -  press k to install your key")
        self.push_screen(AddHost(), added)

    def action_edit_config(self) -> None:
        editor = os.environ.get("EDITOR", "nano")
        with self.suspend():
            subprocess.run(f"{editor} {hosts.SSH_CONFIG}", shell=True)
        self.load_hosts()
        self.status("config reloaded")

    def on_resize(self, event) -> None:
        self.apply_layout(event.size.width)

    def apply_layout(self, width: int) -> None:
        """Below this width the table and the detail pane are both too narrow
        to read, so the detail pane gives way."""
        if self.detail_pinned:
            return
        # Below this the sidebar and the screen preview cannot both be read,
        # and the preview is the part you came for.
        cramped = width < 84
        if cramped == self.sidebar_hidden:
            return
        self.sidebar_hidden = cramped
        self.query_one("#sidebar").set_class(cramped, "hidden")

    def action_toggle_sidebar(self) -> None:
        self.detail_pinned = True
        self.sidebar_hidden = not self.sidebar_hidden
        self.query_one("#sidebar").set_class(self.sidebar_hidden, "hidden")
        if not self.sidebar_hidden:
            self.query_one("#hosts", HostTable).focus()
        self.status("sidebar hidden -- s brings it back"
                    if self.sidebar_hidden else "sidebar shown")

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.focus()

    def action_clear_filter(self) -> None:
        reply = self.query_one("#reply", Input)
        if reply.has_focus:
            reply.value = ""
            self.query_one("#hosts", HostTable).focus()
            self.status(self.summary())
            return
        box = self.query_one("#filter", Input)
        box.value = ""
        box.remove_class("visible")
        self.query_text = ""
        self.render_table()
        self.render_detail()
        self.query_one("#hosts", HostTable).focus()
        self.status(self.summary())

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.query_text = event.value
            self.render_table()
            self.render_detail()
            if event.value:
                self.status(f"filter '{event.value}' -- "
                            f"{len(self.listed)} of {len(self.order)}"
                            "  -  esc clears it")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.query_one("#hosts", HostTable).focus()
        elif event.input.id == "reply":
            self.send_reply(event.value)
            event.input.value = ""
