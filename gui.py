"""helm as a window: the session list beside the session itself.

The panel used to hand its terminal to tmux, or throw a new window at the
compositor and hope it landed somewhere sensible. Neither is a layout. Here the
list is a sidebar and the session opens in a terminal widget next to it, so
opening a chat costs you nothing you were already looking at.

Only the front is new. hosts.py and agent_state.py are untouched: the probe,
the stream, the states and connect_argv() never knew what was drawing them.

GTK4 rather than Qt because VTE -- the widget GNOME Terminal, Tilix and Ptyxis
are built on -- is a real terminal emulator with first-class Python bindings.
Qt's equivalent is Konsole's, which has none.
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")

from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # noqa: E402

import agent_state  # noqa: E402
import hosts  # noqa: E402
from theming import load_palette  # noqa: E402

APP_ID = "org.omarchy.helm"

REFRESH_SECONDS = 45     # the full sweep: uptime, disk, the session list
WATCH_INTERVAL = 1.0     # how often the far side re-dumps a screen
WATCH_RETRY_MIN = 3.0
WATCH_RETRY_MAX = 300.0
WATCH_HEALTHY = 30.0     # a stream that lasted this long was not a refusal

# The mark against each session, in the order the eye should find them: the
# ones waiting on you first. Same vocabulary as the TUI, so a state means the
# same thing in either front end.
MARKS = {
    agent_state.NEEDS_YOU: "!",
    agent_state.DRAFT: "!",
    agent_state.WORKING: "*",
    agent_state.READY: "o",
    agent_state.SHELL: ".",
    agent_state.UNKNOWN: "?",
}


def mark_colour(palette, state: str) -> str:
    if state in (agent_state.NEEDS_YOU, agent_state.DRAFT):
        return palette.red
    if state == agent_state.WORKING:
        return palette.yellow
    if state == agent_state.READY:
        return palette.green
    return palette.muted


def rgba(colour: str) -> Gdk.RGBA:
    value = Gdk.RGBA()
    value.parse(colour)
    return value


class Session(Gtk.Box):
    """One open session: a VTE terminal with the real thing running in it.

    The argv comes from hosts.connect_argv(), which is the same call the TUI
    makes -- so a local session and a remote one differ only in what that
    returns, and neither knows it is being drawn into a widget instead of
    taking over a terminal.
    """

    def __init__(self, host: str, name: str, palette, on_exit):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host = host
        self.name = name
        self.on_exit = on_exit

        self.term = Vte.Terminal()
        self.term.set_hexpand(True)
        self.term.set_vexpand(True)
        self.term.set_scrollback_lines(10000)
        self.term.set_font(Pango.FontDescription.from_string("monospace 11"))
        self.term.set_colors(rgba(palette.foreground),
                             rgba(palette.background), None)
        self.term.set_mouse_autohide(True)
        self.term.connect("child-exited", self._exited)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.term)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        self.append(scroller)

        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.path.expanduser("~"),
            hosts.connect_argv(host, name),
            None,
            # SEARCH_PATH because the argv names ssh-connect and bash rather
            # than spelling out where they live.
            GLib.SpawnFlags.SEARCH_PATH,
            # The introspected signature omits child_setup_data, but the
            # marshaller wants it: the positional list here is the C one.
            None,      # child_setup
            None,      # child_setup_data
            -1,        # no timeout: a slow host is not a failed spawn
            None,      # cancellable
            self._spawned,
        )

    def _spawned(self, _term, pid, error, _data=None):
        """A spawn that fails leaves an empty black rectangle and no clue.

        Saying so in the terminal itself puts the reason where the session
        would have been, which is where you are already looking.
        """
        if error is not None:
            self.term.feed(f"\r\n  could not start {self.host}/{self.name}:"
                           f"\r\n  {error.message}\r\n".encode())

    def _exited(self, _term, _status):
        self.on_exit(self)


class Helm(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="helm")
        self.set_default_size(1400, 860)

        self.palette = load_palette()
        self.rows: dict[str, dict] = {}
        self.order: list[str] = []
        self.open: dict[tuple[str, str], Session] = {}
        # What alt-1..9 reaches, rebuilt with the list. Numbering what is on
        # screen beats numbering what happens to be open: the number is there
        # before you open it, which is when you need it.
        self.slots: list[tuple[str, str]] = []
        # What the sidebar is currently made of, and the labels inside each
        # session row. Kept so a screen changing can repaint the words without
        # rebuilding the widgets under the pointer -- see render().
        self.shape: list = []
        self.widgets: dict[tuple[str, str], dict] = {}
        self.watched: set[str] = set()
        self.watchers: dict[str, object] = {}
        self.inflight: set[str] = set()
        self.frames: queue.Queue = queue.Queue(maxsize=256)
        self.stopping = False

        self._build()
        self._style()
        self._shortcuts()

        self.load_hosts()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.sweep_tick)
        # The stream can speak several times a second on a busy host. Draining
        # it on a timer rather than on arrival keeps the list from rebuilding
        # under its own feet while you are trying to click something.
        GLib.timeout_add(400, self.drain_frames)

        self.connect("close-request", self.shut_down)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        header = Gtk.HeaderBar()
        self.subtitle = Gtk.Label(label="")
        self.subtitle.add_css_class("subtitle")
        header.set_title_widget(self.subtitle)

        new = Gtk.Button(icon_name="list-add-symbolic")
        new.set_tooltip_text("New session on the selected host")
        new.connect("clicked", lambda _b: self.prompt_new_session())
        header.pack_start(new)

        add = Gtk.Button(icon_name="network-server-symbolic")
        add.set_tooltip_text("Add a server to ~/.ssh/config")
        add.connect("clicked", lambda _b: self.prompt_add_host())
        header.pack_start(add)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh every host now")
        refresh.connect("clicked", lambda _b: self.load_hosts())
        header.pack_end(refresh)

        guide = Gtk.MenuButton(icon_name="help-about-symbolic")
        guide.set_tooltip_text("What the marks mean, and the keys")
        guide.set_popover(self._guide())
        header.pack_end(guide)
        self.set_titlebar(header)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated", self.row_activated)
        self.list.add_css_class("sidebar")

        side = Gtk.ScrolledWindow()
        side.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side.set_child(self.list)
        side.set_size_request(300, -1)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)

        self.placeholder = Gtk.Label(
            label="Pick a session on the left.\n"
                  "It opens here -- this window keeps the list.\n\n"
                  "alt-1..9  the numbered session on the left\n"
                  "ctrl-tab  next open session\n"
                  "F12       back to the list")
        self.placeholder.add_css_class("placeholder")
        self.placeholder.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.placeholder, "placeholder")

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_start_child(side)
        split.set_end_child(self.stack)
        split.set_position(300)
        split.set_resize_start_child(False)
        self.set_child(split)

    def _guide(self) -> Gtk.Popover:
        """What a mark means, spelled out.

        A single red ! is only obvious once someone has told you; until then
        it is a punctuation mark on a list.
        """
        marks = [
            ("!", agent_state.NEEDS_YOU, "needs you",
             "blocked on a prompt only you can answer"),
            ("!", agent_state.DRAFT, "unsent draft",
             "text left in the box, never submitted -- looks done, is not"),
            ("*", agent_state.WORKING, "working",
             "busy, or running background agents -- leave it"),
            ("o", agent_state.READY, "idle", "waiting at an empty prompt"),
            (".", agent_state.SHELL, "shell", "not an agent, just a shell"),
            ("?", agent_state.UNKNOWN, "unknown",
             "not recognised -- never assume this one is idle"),
        ]
        grid = Gtk.Grid(row_spacing=4, column_spacing=10, margin_top=12,
                        margin_bottom=12, margin_start=12, margin_end=12)
        for row, (mark, state, name, means) in enumerate(marks):
            glyph = Gtk.Label(label=mark, xalign=0)
            glyph.add_css_class("mark")
            glyph.set_attributes(self._colour_attrs(
                mark_colour(self.palette, state)))
            grid.attach(glyph, 0, row, 1, 1)
            grid.attach(Gtk.Label(label=name, xalign=0), 1, row, 1, 1)
            hint = Gtk.Label(label=means, xalign=0)
            hint.add_css_class("detail")
            grid.attach(hint, 2, row, 1, 1)

        keys = [("alt-1..9", "open the session with that number"),
                ("ctrl-tab", "next open session"),
                ("F12", "back to the list"),
                ("ctrl-shift-w", "close the view, leave the session running"),
                ("right-click", "kill a session, or open it")]
        for offset, (key, means) in enumerate(keys):
            row = len(marks) + offset + 1
            label = Gtk.Label(label=key, xalign=0)
            label.add_css_class("tab")
            grid.attach(label, 0, row, 2, 1)
            hint = Gtk.Label(label=means, xalign=0)
            hint.add_css_class("detail")
            grid.attach(hint, 2, row, 1, 1)

        popover = Gtk.Popover()
        popover.set_child(grid)
        return popover

    # -- acting on a session -----------------------------------------------

    def selected_key(self) -> tuple[str, str] | None:
        row = self.list.get_selected_row()
        return getattr(row, "key", None) if row is not None else None

    def selected_host(self) -> str:
        """The host of whatever is selected, or the first one listed.

        A session row implies its host, so "new session" does not need a
        separate host selection to mean something.
        """
        key = self.selected_key()
        if key:
            return key[0]
        return self.order[0] if self.order else hosts.LOCAL

    def row_menu(self, row, x: float, y: float) -> None:
        key = getattr(row, "key", None)
        if key is None:
            return
        host, name = key

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover()
        # Parented to the list, not to the row: rows are replaced whenever the
        # session list itself changes, and a popover whose parent is destroyed
        # goes with it -- which looked like the menu refusing to be clicked.
        popover.set_parent(self.list)
        point = Gdk.Rectangle()
        allocation = row.get_allocation()
        point.x = allocation.x + int(x)
        point.y = allocation.y + int(y)
        point.width = point.height = 1
        popover.set_pointing_to(point)
        popover.connect("closed", lambda pop: pop.unparent())

        def item(label, handler, destructive=False):
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_xalign(0)
            if destructive:
                button.add_css_class("destructive")
            button.connect("clicked", lambda _b: (popover.popdown(), handler()))
            box.append(button)

        item("Open", lambda: self.open_session(host, name))
        item(f"Kill {host}/{name}...", lambda: self.confirm_kill(host, name),
             destructive=True)
        popover.set_child(box)
        popover.popup()

    def confirm_kill(self, host: str, name: str) -> None:
        """Ask first. A killed session takes whatever it was doing with it,
        and there is no scrollback afterwards to find out what that was."""
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Kill {host}/{name}?")
        dialog.set_detail("Everything running in the session ends. This cannot "
                          "be undone, and the screen is not kept.")
        dialog.set_buttons(["Cancel", "Kill session"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(source, result):
            try:
                choice = source.choose_finish(result)
            except GLib.Error:
                return
            if choice == 1:
                threading.Thread(target=self._kill, args=(host, name),
                                 daemon=True).start()

        dialog.choose(self, None, answered)

    def _kill(self, host: str, name: str) -> None:
        try:
            hosts.kill_session(host, name)
        except (hosts.HostError, OSError) as exc:
            GLib.idle_add(self.complain, f"{host}/{name}", str(exc))
            return
        # The view is of a session that no longer exists; the terminal will
        # notice on its own when the client exits, but closing it now is what
        # you asked for.
        session = self.open.get((host, name))
        if session is not None:
            GLib.idle_add(self.close_session, session)
        GLib.idle_add(self.sweep)

    def complain(self, what: str, message: str) -> bool:
        dialog = Gtk.AlertDialog()
        dialog.set_message(what)
        dialog.set_detail(message)
        dialog.show(self)
        return GLib.SOURCE_REMOVE

    # -- new session, new host ---------------------------------------------

    def prompt_new_session(self) -> None:
        host = self.selected_host()
        self.ask(f"New session on {host}", [("Name", "shell")],
                 lambda values: self.open_session(host, values["Name"]))

    def prompt_add_host(self) -> None:
        def add(values):
            try:
                backup = hosts.add_host(values["Name"], values["Hostname"],
                                        values["User"], values["Port"] or "22")
            except (hosts.HostError, OSError) as exc:
                self.complain("Could not add host", str(exc))
                return
            self.load_hosts()
            self.complain("Added to ~/.ssh/config",
                          f"The old file is kept at {backup.name}.")

        self.ask("Add a server", [("Name", ""), ("Hostname", ""),
                                  ("User", ""), ("Port", "22")], add)

    def ask(self, title: str, fields: list[tuple[str, str]], done) -> None:
        """A small modal form. GTK has no one-line prompt, and four of these
        are cheaper than four hand-built dialogs."""
        window = Gtk.Window(title=title, transient_for=self, modal=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=14, margin_bottom=14,
                      margin_start=14, margin_end=14)
        entries: dict[str, Gtk.Entry] = {}
        for label, initial in fields:
            line = Gtk.Box(spacing=8)
            name = Gtk.Label(label=label, xalign=0)
            name.set_size_request(90, -1)
            entry = Gtk.Entry()
            entry.set_text(initial)
            entry.set_hexpand(True)
            entries[label] = entry
            line.append(name)
            line.append(entry)
            box.append(line)

        def submit(*_args):
            values = {k: e.get_text().strip() for k, e in entries.items()}
            window.close()
            if any(values.values()):
                done(values)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: window.close())
        confirm = Gtk.Button(label=title.split()[0])
        confirm.add_css_class("suggested-action")
        confirm.connect("clicked", submit)
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        for entry in entries.values():
            entry.connect("activate", submit)   # enter submits from any field
        window.set_child(box)
        window.present()

    def _shortcuts(self) -> None:
        """Keys for switching, taken before the terminal sees them.

        CAPTURE phase is the whole point: a focused VTE swallows almost
        everything, and alt-1 in particular it would send on as ESC-1. The
        window has to claim these on the way down or they never arrive.

        That means F12 no longer reaches tmux, where ssh-connect binds it to
        detach. In a window with a session list down the side there is nothing
        to detach back to -- so it does the same job one level up, and puts you
        back on the list.
        """
        keys = Gtk.ShortcutController()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(keys)

        def bind(accel: str, handler):
            keys.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(accel),
                action=Gtk.CallbackAction.new(handler)))

        for n in range(1, 10):
            bind(f"<alt>{n}",
                 lambda _w, _a, index=n - 1: self.show_nth(index))
        bind("<ctrl>Tab", lambda _w, _a: self.cycle(1))
        bind("<ctrl><shift>Tab", lambda _w, _a: self.cycle(-1))
        bind("<ctrl>Page_Down", lambda _w, _a: self.cycle(1))
        bind("<ctrl>Page_Up", lambda _w, _a: self.cycle(-1))
        bind("F12", lambda _w, _a: self.focus_list())
        bind("<ctrl><shift>w", lambda _w, _a: self.close_visible())

    # -- switching ---------------------------------------------------------

    def opened(self) -> list[Session]:
        """Open sessions in the order they were opened -- which is the order
        alt-1..9 counts in, and the order the list marks them."""
        return list(self.open.values())

    def show(self, session: Session) -> bool:
        self.stack.set_visible_child(session)
        session.term.grab_focus()
        self.select_key((session.host, session.name))
        return True

    def show_nth(self, index: int) -> bool:
        if index < len(self.slots):
            self.open_session(*self.slots[index])
        return True     # claimed either way, or alt-4 types a 4 in the shell

    def cycle(self, step: int) -> bool:
        sessions = self.opened()
        if not sessions:
            return True
        current = self.stack.get_visible_child()
        try:
            index = sessions.index(current)
        except ValueError:
            index = 0
        return self.show(sessions[(index + step) % len(sessions)])

    def focus_list(self) -> bool:
        """Back to the list, without reaching for the mouse. Arrows move,
        enter opens."""
        self.list.grab_focus()
        row = self.list.get_selected_row()
        if row is not None:
            row.grab_focus()
        return True

    def close_visible(self) -> bool:
        """Close the view. The tmux session on the other side keeps running --
        that is what it is for -- so this loses nothing but the tab."""
        current = self.stack.get_visible_child()
        if isinstance(current, Session):
            self.close_session(current)
        return True

    def select_key(self, key: tuple[str, str]) -> None:
        index = 0
        while (row := self.list.get_row_at_index(index)) is not None:
            if getattr(row, "key", None) == key:
                self.list.select_row(row)
                return
            index += 1

    def _style(self) -> None:
        """Take the desktop's colours rather than GTK's.

        The panel sits next to terminals all day; matching Omarchy's theme is
        what stops it reading as a foreign application.
        """
        p = self.palette
        css = f"""
        window {{ background: {p.background}; color: {p.foreground}; }}
        headerbar {{ background: {p.panel}; color: {p.foreground};
                     border-bottom: 1px solid {p.surface}; }}
        .subtitle {{ color: {p.muted}; font-size: 0.9em; }}
        .sidebar {{ background: {p.panel}; }}
        .sidebar row {{ padding: 2px 10px; }}
        .sidebar row:selected {{ background: {p.accent}; color: {p.background}; }}
        .host {{ color: {p.muted}; font-weight: bold;
                 padding-top: 10px; letter-spacing: 0.06em; }}
        .mark {{ font-family: monospace; font-weight: bold; }}
        .name {{ font-family: monospace; }}
        .detail {{ color: {p.muted}; font-size: 0.85em; }}
        .placeholder {{ color: {p.muted}; }}
        .tab {{ color: {p.accent}; font-size: 0.8em;
                font-family: monospace; }}
        .slot {{ color: {p.muted}; font-family: monospace; font-size: 0.85em; }}
        .slot-open {{ color: {p.accent}; font-weight: bold; }}
        .destructive {{ color: {p.red}; }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # -- the list ----------------------------------------------------------

    def load_hosts(self) -> None:
        self.order = hosts.list_hosts()
        for host in self.order:
            self.rows.setdefault(host, hosts.blank(host))
        for gone in [h for h in self.rows if h not in self.order]:
            del self.rows[gone]

        self.render()
        if os.environ.get("HELM_NO_WATCH") != "1":
            for host in self.order:
                if host not in self.watched:
                    self.watched.add(host)
                    threading.Thread(target=self._stream, args=(host,),
                                     name=f"watch-{host}", daemon=True).start()
        self.sweep()

    def render(self) -> None:
        """Bring the sidebar up to date, rebuilding it only if it has to.

        A screen changing is the common case, and it changes words, not
        structure. Rebuilding the rows for that destroys whatever the pointer
        or the keyboard was on -- a right-click menu is parented to a row, so
        it vanished a frame after opening -- so the shape is compared first
        and only a real change to it costs new widgets.
        """
        shape = []
        waiting = 0
        slots = []
        for host in self.order:
            shape.append(("host", host, self.rows[host]["state"]))
            for session in self.rows[host]["sessions"]:
                if session["agent"]["state"] in (agent_state.NEEDS_YOU,
                                                 agent_state.DRAFT):
                    waiting += 1
                slots.append((host, session["name"]))
                shape.append(("session", host, session["name"]))

        self.slots = slots
        self.subtitle.set_text(
            f"{waiting} waiting on you" if waiting else "nothing waiting")

        if shape == self.shape:
            self.repaint()
            return

        chosen = None
        row = self.list.get_selected_row()
        if row is not None:
            chosen = getattr(row, "key", None)

        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)
        self.widgets.clear()

        slot = 0
        for host in self.order:
            self.list.append(self._host_row(host, self.rows[host]))
            for session in self.rows[host]["sessions"]:
                slot += 1
                self.list.append(self._session_row(host, session, slot))
        self.shape = shape

        if chosen is not None:
            self.select_key(chosen)

    def repaint(self) -> None:
        """Same rows, new words: marks, states and which ones are open."""
        for host in self.order:
            for session in self.rows[host]["sessions"]:
                parts = self.widgets.get((host, session["name"]))
                if parts is None:
                    continue
                state = session["agent"]["state"]
                parts["mark"].set_label(MARKS.get(state, "?"))
                parts["mark"].set_attributes(self._colour_attrs(
                    mark_colour(self.palette, state)))
                parts["detail"].set_label(self._detail(session))
                open_now = (host, session["name"]) in self.open
                if open_now:
                    parts["number"].add_css_class("slot-open")
                else:
                    parts["number"].remove_css_class("slot-open")

    @staticmethod
    def _detail(session: dict) -> str:
        detail = session["agent"]["label"]
        if session["agent"]["detail"]:
            detail = f"{detail}  {session['agent']['detail']}"
        return detail

    def _host_row(self, host: str, data: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        # A host is a heading, not a destination: there is nothing to open on
        # it, so it neither highlights nor answers a click.
        row.key = None
        row.set_activatable(False)
        row.set_selectable(False)
        box = Gtk.Box(spacing=8)
        label = Gtk.Label(label=host, xalign=0)
        label.add_css_class("host")
        box.append(label)

        note = {"down": "down", "nokey": "no key"}.get(data["state"], "")
        if not note and data["state"] == "unknown":
            note = "checking"
        if note:
            tail = Gtk.Label(label=note, xalign=0)
            tail.add_css_class("detail")
            box.append(tail)
        row.set_child(box)
        return row

    def _session_row(self, host: str, session: dict,
                     slot: int) -> Gtk.ListBoxRow:
        state = session["agent"]["state"]
        row = Gtk.ListBoxRow()
        row.key = (host, session["name"])

        box = Gtk.Box(spacing=8)
        # The number is the alt-key. Past nine there is no key to name, so the
        # column stays blank rather than promising one.
        number = Gtk.Label(label=str(slot) if slot < 10 else "", xalign=1)
        number.add_css_class("slot")
        if (host, session["name"]) in self.open:
            number.add_css_class("slot-open")
        number.set_size_request(14, -1)
        box.append(number)

        mark = Gtk.Label(label=MARKS.get(state, "?"))
        mark.add_css_class("mark")
        mark.set_size_request(12, -1)
        colour = mark_colour(self.palette, state)
        mark.set_attributes(self._colour_attrs(colour))
        box.append(mark)

        name = Gtk.Label(label=session["name"], xalign=0)
        name.add_css_class("name")
        box.append(name)

        tail = Gtk.Label(label=self._detail(session), xalign=1)
        tail.add_css_class("detail")
        tail.set_hexpand(True)
        tail.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(tail)

        self.widgets[(host, session["name"])] = {
            "number": number, "mark": mark, "detail": tail}

        menu = Gtk.GestureClick()
        menu.set_button(3)
        menu.connect("pressed",
                     lambda _g, _n, x, y, r=row: self.row_menu(r, x, y))
        row.add_controller(menu)

        row.set_child(box)
        return row

    @staticmethod
    def _colour_attrs(colour: str) -> Pango.AttrList:
        attrs = Pango.AttrList()
        value = rgba(colour)
        attrs.insert(Pango.attr_foreground_new(
            int(value.red * 65535), int(value.green * 65535),
            int(value.blue * 65535)))
        return attrs

    # -- opening one -------------------------------------------------------

    def row_activated(self, _list, row) -> None:
        key = getattr(row, "key", None)
        if key is None:
            return
        self.open_session(*key)

    def open_session(self, host: str, name: str) -> None:
        key = (host, name)
        if key in self.open:
            self.stack.set_visible_child(self.open[key])
            self.open[key].term.grab_focus()
            return

        session = Session(host, name, self.palette, self.close_session)
        self.open[key] = session
        self.stack.add_named(session, f"{host}/{name}")
        self.stack.set_visible_child(session)
        session.term.grab_focus()

    def close_session(self, session: Session) -> None:
        """A session whose command ended takes its terminal with it.

        The tmux session on the other side is untouched -- that is the whole
        point of it -- so this is closing a view, not ending any work.
        """
        self.open.pop((session.host, session.name), None)
        self.stack.remove(session)
        if not self.open:
            self.stack.set_visible_child(self.placeholder)

    # -- probing -----------------------------------------------------------

    def sweep_tick(self) -> bool:
        self.sweep()
        return GLib.SOURCE_CONTINUE

    def sweep(self) -> None:
        for host in self.order:
            if host in self.inflight:
                continue
            self.inflight.add(host)
            threading.Thread(target=self._probe, args=(host,),
                             name=f"probe-{host}", daemon=True).start()

    def _probe(self, host: str) -> None:
        row = hosts.probe(host)
        GLib.idle_add(self._probed, host, row)

    def _probed(self, host: str, row: dict) -> bool:
        self.inflight.discard(host)
        if host in self.rows:
            self.rows[host] = row
            self.render()
        return GLib.SOURCE_REMOVE

    # -- streaming ---------------------------------------------------------

    def _stream(self, host: str) -> None:
        """Hold one channel open per host and read screens off it.

        Lifted from the TUI unchanged in spirit: the far side only speaks when
        a screen actually changed, so an idle host costs nothing.
        """
        delay = WATCH_RETRY_MIN
        while not self.stopping:
            opened = time.monotonic()
            try:
                proc = hosts.watch_screens(host, WATCH_INTERVAL)
            except OSError:
                return
            self.watchers[host] = proc
            buffer: list[str] = []
            try:
                while not self.stopping:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if line.startswith("###FRAME"):
                        frame = hosts.parse_frame(buffer, host)
                        buffer = []
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

            if self.stopping:
                return
            # A stream that ran a while and dropped is a network event: come
            # back quickly. One that died at once means we are being refused,
            # and trying harder is what turns that into being banned.
            delay = (WATCH_RETRY_MIN if time.monotonic() - opened >= WATCH_HEALTHY
                     else min(delay * 2, WATCH_RETRY_MAX))
            deadline = time.monotonic() + delay * random.uniform(0.75, 1.25)
            while not self.stopping and time.monotonic() < deadline:
                time.sleep(0.25)

    def drain_frames(self) -> bool:
        dirty = False
        while True:
            try:
                host, frame = self.frames.get_nowait()
            except queue.Empty:
                break
            row = self.rows.get(host)
            if row is None:
                continue
            known = {s["name"] for s in row["sessions"]}
            if set(frame) != known:
                # The stream only carries screens, so a session appearing or
                # going away needs a real probe to fill in the rest.
                if host not in self.inflight:
                    self.inflight.add(host)
                    threading.Thread(target=self._probe, args=(host,),
                                     daemon=True).start()
                continue
            for session in row["sessions"]:
                data = frame.get(session["name"])
                if not data:
                    continue
                session["screen"] = data["screen"]
                session["agent"] = agent_state.classify(
                    data["screen"], data["commands"])
                dirty = True
        if dirty:
            self.render()
        return GLib.SOURCE_CONTINUE

    # -- leaving -----------------------------------------------------------

    def shut_down(self, *_args) -> bool:
        self.stopping = True
        for proc in list(self.watchers.values()):
            try:
                proc.terminate()
            except Exception:
                pass
        return False


class HelmApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        window = self.props.active_window or Helm(self)
        window.present()


def run() -> int:
    return HelmApp().run(None)
