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

from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

import agent_state  # noqa: E402
import hosts  # noqa: E402
from theming import load_palette  # noqa: E402

APP_ID = "org.omarchy.helm"
HOME_PAGE = "https://www.ojm.co"
SOURCE = "https://github.com/ojm1/helm-tui"

# U+2388 is, literally, the helm symbol -- a ship's wheel, which is where the
# name comes from. The wordmark under it is box-drawing rather than an image
# so it takes the theme's accent colour like everything else.
# PCRE2 compile flags, which VTE takes as-is. NO_UTF_CHECK because the
# terminal's own contents are the input and it is already UTF-8.
PCRE2_MULTILINE = 0x00000400
PCRE2_UTF = 0x00080000
PCRE2_NO_UTF_CHECK = 0x40000000

# Bare URLs, which no terminal marks up for you. Trailing punctuation is left
# out deliberately: a link at the end of a sentence should not swallow the
# full stop, and brackets around it are not part of it either.
URL_PATTERN = (r"(?:https?://|ftp://|file://|mailto:)"
               r"[^\s<>\"\'`{}|\\^\[\]]*[^\s<>\"\'`{}|\\^\[\].,;:!?)]")

WORDMARK = """╻ ╻┏━╸╻  ┏┳┓
┣━┫┣╸ ┃  ┃┃┃
╹ ╹┗━╸┗━╸╹ ╹"""

REFRESH_SECONDS = 45     # the full sweep: uptime, disk, the session list
WATCH_INTERVAL = 1.0     # how often the far side re-dumps a screen
WATCH_RETRY_MIN = 3.0
WATCH_RETRY_MAX = 300.0
WATCH_HEALTHY = 30.0     # a stream that lasted this long was not a refusal
# How long an unsent draft has to sit untouched before it is worth saying out
# loud. Typing is a draft too -- see draft_settled().
DRAFT_DWELL = 120.0

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

    def __init__(self, host: str, name: str, palette, on_exit, screen=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host = host
        self.name = name
        self.on_exit = on_exit
        # What the far side thinks is on this screen. The panel is already
        # capturing it every second for the state; the menu reads links out of
        # the same text, which is the only copy where a wrapped URL can be put
        # back together.
        self.screen = screen or (lambda: "")

        self.term = Vte.Terminal()
        self.term.set_hexpand(True)
        self.term.set_vexpand(True)
        self.term.set_scrollback_lines(10000)
        self.term.set_font(Pango.FontDescription.from_string("monospace 11"))
        self.term.set_colors(rgba(palette.foreground),
                             rgba(palette.background), None)
        self.term.set_mouse_autohide(True)
        self.term.connect("child-exited", self._exited)

        # OSC 8 hyperlinks, the ones a program marks up itself.
        self.term.set_allow_hyperlink(True)
        # And the ones nobody marked up, which is most of them -- the login
        # URL an agent prints is just text until something matches it.
        self.link_tag = -1
        try:
            regex = Vte.Regex.new_for_match(
                URL_PATTERN, -1,
                PCRE2_UTF | PCRE2_NO_UTF_CHECK | PCRE2_MULTILINE)
            self.link_tag = self.term.match_add_regex(regex, 0)
            self.term.match_set_cursor_name(self.link_tag, "pointer")
        except Exception:
            pass    # no link detection is a worse terminal, not a broken one

        clicks = Gtk.GestureClick()
        clicks.set_button(0)          # any button; the handler sorts them out
        clicks.connect("pressed", self._clicked)
        self.term.add_controller(clicks)

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

    def link_at(self, x: float, y: float) -> str:
        """The URL under the pointer, marked up or not."""
        try:
            hyperlink = self.term.check_hyperlink_at(x, y)
        except Exception:
            hyperlink = None
        if hyperlink:
            return hyperlink
        try:
            match, _tag = self.term.check_match_at(x, y)
        except Exception:
            return ""
        return match or ""

    def _clicked(self, gesture, _presses, x, y):
        """ctrl-click opens a link; the plain click stays the terminal's.

        A modifier rather than a bare click because the program inside may be
        tracking the mouse itself, and taking its clicks away would be worse
        than not having links.
        """
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)

        if button == 2:                       # middle: the X primary paste
            self.term.paste_primary()
            return
        if button == 3:
            self.menu(x, y)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return
        if button == 1 and control:
            uri = self.link_at(x, y)
            if uri:
                Gtk.UriLauncher(uri=uri).launch(None, None, None, None)
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def menu(self, x: float, y: float) -> None:
        """Copy, paste, and whatever link is under the pointer."""
        uri = self.link_at(x, y)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover()
        popover.set_parent(self.term)
        point = Gdk.Rectangle()
        point.x, point.y = int(x), int(y)
        point.width = point.height = 1
        popover.set_pointing_to(point)
        popover.connect("closed", lambda pop: pop.unparent())

        def item(label, handler, enabled=True):
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_xalign(0)
            button.set_sensitive(enabled)
            button.connect("clicked",
                           lambda _b: (popover.popdown(), handler()))
            box.append(button)

        if uri:
            item(f"Open {uri[:44]}",
                 lambda: Gtk.UriLauncher(uri=uri).launch(None, None, None, None))
            item("Copy link", lambda: self.to_clipboard(uri))

        # Links the pointer is not on, and long ones the terminal cannot see
        # as links at all because tmux delivered them in pieces.
        for link in [u for u in hosts.screen_links(self.screen())
                     if u != uri][:5]:
            shown = link if len(link) <= 44 else link[:41] + "..."
            item(f"Open {shown}",
                 lambda u=link: Gtk.UriLauncher(uri=u).launch(None, None, None, None))
            item(f"Copy {shown}", lambda u=link: self.to_clipboard(u))
        item("Copy", self.copy, self.term.get_has_selection())
        item("Paste", self.paste)
        item("Select all", self.term.select_all)
        popover.set_child(box)
        popover.popup()

    @staticmethod
    def to_clipboard(text: str) -> None:
        Gdk.Display.get_default().get_clipboard().set(text)

    def copy(self) -> None:
        if self.term.get_has_selection():
            self.term.copy_clipboard_format(Vte.Format.TEXT)

    def paste(self) -> None:
        self.term.paste_clipboard()

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
        # What each session was last seen doing. A notification is worth
        # sending when this changes to one of the states that means you, and
        # never for a state that was already true when helm started.
        self.was: dict[tuple[str, str], str] = {}
        # Per session, the draft last seen in its box, when it stopped
        # changing, and whether that one has been announced already.
        self.drafts: dict[tuple[str, str], tuple[str, float, bool]] = {}
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
        self.check_agent()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.sweep_tick)
        # The stream can speak several times a second on a busy host. Draining
        # it on a timer rather than on arrival keeps the list from rebuilding
        # under its own feet while you are trying to click something.
        GLib.timeout_add(400, self.drain_frames)

        self.connect("close-request", self.shut_down)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        # No title bar at all. GTK hides it when the window is fullscreened,
        # so nothing that matters can live there -- and once the actions moved
        # into the sidebar it was an empty strip taking a row of pixels for
        # the name of a window whose name is drawn six lines below it.
        # Closing is the x in the footer, ctrl-q, or the compositor.
        self.set_decorated(False)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated", self.row_activated)
        self.list.add_css_class("sidebar")
        self.list.set_filter_func(self.matches)

        self.filter = Gtk.SearchEntry()
        self.filter.set_placeholder_text("filter")
        self.filter.add_css_class("filter")
        self.filter.connect("search-changed",
                            lambda _e: self.list.invalidate_filter())
        self.filter.connect("stop-search", lambda _e: self.clear_filter())

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list)
        scroller.set_vexpand(True)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.add_css_class("side")
        side.set_size_request(300, -1)
        side.append(self._wordmark())
        side.append(self.filter)
        side.append(scroller)
        side.append(self._footer())

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
                ("ctrl-shift-k", "kill the selected session, for good"),
                ("ctrl-f", "filter the list, esc clears it"),
                ("F11 / ctrl-q", "fullscreen / quit")]
        for offset, (key, means) in enumerate(keys):
            row = len(marks) + offset + 1
            label = Gtk.Label(label=key, xalign=0)
            label.add_css_class("tab")
            grid.attach(label, 0, row, 2, 1)
            hint = Gtk.Label(label=means, xalign=0)
            hint.add_css_class("detail")
            grid.attach(hint, 2, row, 1, 1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(grid)

        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.set_margin_start(12)
        rule.set_margin_end(12)
        box.append(rule)

        credits = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          margin_bottom=12, margin_start=12, margin_end=12)
        blurb = Gtk.Label(
            label="helm -- the servers you keep coding agents on, and whether "
                  "they are working or waiting on you.", xalign=0)
        blurb.add_css_class("detail")
        blurb.set_wrap(True)
        blurb.set_max_width_chars(46)
        credits.append(blurb)

        links = Gtk.Box(spacing=12, margin_top=4)
        for label, uri in (("www.ojm.co", HOME_PAGE), ("source", SOURCE)):
            link = Gtk.LinkButton(uri=uri, label=label)
            link.set_has_frame(False)
            link.add_css_class("link")
            links.append(link)
        credits.append(links)

        holder = Gtk.Label(label="(c) 2026 Owen McCrink  --  MIT", xalign=0)
        holder.add_css_class("detail")
        credits.append(holder)
        box.append(credits)

        popover = Gtk.Popover()
        popover.set_child(box)
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

        self.popup(row, x, y, [
            ("Open", lambda: self.open_session(host, name), False),
            (f"Kill {host}/{name}...",
             lambda: self.confirm_kill(host, name), True),
        ])

    def host_menu(self, row, host: str, x: float, y: float) -> None:
        items = [("New session...", lambda: self.prompt_new_session(host), False),
                 ("Passwords and keys...", lambda: self.show_secrets(host), False)]
        if hosts.is_local(host):
            items.append((f"Open {hosts.files_label(host)}",
                          lambda: hosts.open_files(str(hosts.files_root(host))),
                          False))
        elif hosts.is_mounted(host):
            items.append((f"Open {hosts.files_label(host)}",
                          lambda: hosts.open_files(str(hosts.files_root(host))),
                          False))
            items.append(("Unmount", lambda: self.do_mount(host, False), False))
        else:
            items.append(("Mount files over sshfs",
                          lambda: self.do_mount(host, True), False))
            items.append(("Install my key (ssh-copy-id)",
                          lambda: self.install_key(host), False))
        self.popup(row, x, y, items)

    def do_mount(self, host: str, mount: bool) -> None:
        """sshfs on a worker: fusermount blocks for seconds on a busy mount,
        and doing that on the main thread freezes the window."""
        def work():
            try:
                if mount:
                    path = hosts.mount(host)
                    GLib.idle_add(hosts.open_files, path)
                else:
                    hosts.unmount(host)
            except (hosts.HostError, OSError) as exc:
                GLib.idle_add(self.complain, host, str(exc))
                return
            GLib.idle_add(self.render)
        threading.Thread(target=work, daemon=True).start()

    def install_key(self, host: str) -> None:
        try:
            hosts.copy_key(host)
        except hosts.HostError as exc:
            self.complain(host, str(exc))

    def popup(self, row, x: float, y: float, items) -> None:
        """One popover for both menus, parented to the list rather than to a
        row -- rows are replaced when the session list changes, and a popover
        whose parent goes with them cannot be clicked."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover()
        popover.set_parent(self.list)
        point = Gdk.Rectangle()
        allocation = row.get_allocation()
        point.x = allocation.x + int(x)
        point.y = allocation.y + int(y)
        point.width = point.height = 1
        popover.set_pointing_to(point)
        popover.connect("closed", lambda pop: pop.unparent())

        for label, handler, destructive in items:
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_xalign(0)
            if destructive:
                button.add_css_class("destructive")
            button.connect("clicked",
                           lambda _b, h=handler: (popover.popdown(), h()))
            box.append(button)
        popover.set_child(box)
        popover.popup()

    # -- what is filed against a host --------------------------------------

    def show_secrets(self, host: str) -> None:
        """Whatever you keep for this host, read out of the desktop keyring.

        Nothing is stored by helm and nothing is held in memory once the
        window closes: a value is fetched when you ask to see it and the
        field is emptied again on Hide. The keyring is already unlocked by
        your login password, which is the single password this all hangs on.
        """
        window = Gtk.Window(title=f"{host} -- passwords and keys",
                            transient_for=self, modal=True)
        window.set_default_size(460, 320)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)

        listing = Gtk.ListBox()
        listing.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(listing)
        scroller.set_vexpand(True)
        outer.append(scroller)

        def refill():
            while (child := listing.get_first_child()) is not None:
                listing.remove(child)
            names = hosts.secret_names(host)
            if not names:
                empty = Gtk.Label(
                    label="Nothing kept for this host yet.", xalign=0)
                empty.add_css_class("detail")
                listing.append(empty)
            for name in names:
                listing.append(entry_row(name))

        def entry_row(name: str) -> Gtk.Widget:
            line = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4)
            title = Gtk.Label(label=name, xalign=0)
            title.set_size_request(110, -1)
            line.append(title)

            shown = Gtk.Label(label="\u2022" * 8, xalign=0, selectable=True)
            shown.add_css_class("secret")
            shown.set_hexpand(True)
            shown.set_ellipsize(Pango.EllipsizeMode.END)
            line.append(shown)

            reveal = Gtk.Button(label="Show")

            def toggle(_button):
                if reveal.get_label() == "Show":
                    try:
                        shown.set_label(hosts.secret_value(host, name))
                    except hosts.HostError as exc:
                        self.complain(f"{host}/{name}", str(exc))
                        return
                    reveal.set_label("Hide")
                else:
                    shown.set_label("\u2022" * 8)
                    reveal.set_label("Show")

            reveal.connect("clicked", toggle)
            line.append(reveal)

            copy = Gtk.Button(label="Copy")

            def to_clipboard(_button):
                try:
                    value = hosts.secret_value(host, name)
                except hosts.HostError as exc:
                    self.complain(f"{host}/{name}", str(exc))
                    return
                clipboard = Gdk.Display.get_default().get_clipboard()
                clipboard.set(value)
                copy.set_label("Copied")
                # Cleared again shortly: a password left on the clipboard is
                # readable by anything that asks for it.
                def wipe():
                    if clipboard.get_content() is not None:
                        clipboard.set("")
                    copy.set_label("Copy")
                    return False
                GLib.timeout_add_seconds(30, wipe)

            copy.connect("clicked", to_clipboard)
            line.append(copy)

            remove = Gtk.Button(label="Forget")
            remove.add_css_class("destructive")

            def forget(_button):
                try:
                    hosts.secret_clear(host, name)
                except hosts.HostError as exc:
                    self.complain(f"{host}/{name}", str(exc))
                    return
                refill()

            remove.connect("clicked", forget)
            line.append(remove)
            return line

        def add(_button):
            def store(values):
                try:
                    hosts.secret_store(host, values["Name"], values["Value"])
                except hosts.HostError as exc:
                    self.complain("Could not store", str(exc))
                    return
                refill()
            self.ask(f"Keep for {host}", [("Name", ""), ("Value", "")], store,
                     secret="Value")

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        new = Gtk.Button(label="Add...")
        new.connect("clicked", add)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda _b: window.close())
        buttons.append(new)
        buttons.append(close)
        outer.append(buttons)

        note = Gtk.Label(
            label="Kept in the desktop keyring, which your login password "
                  "unlocks. helm stores nothing itself.", xalign=0)
        note.add_css_class("detail")
        note.set_wrap(True)
        outer.append(note)

        refill()
        window.set_child(outer)
        window.present()
        return window

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

    def prompt_new_session(self, host: str | None = None) -> None:
        host = host or self.selected_host()
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

    def ask(self, title: str, fields: list[tuple[str, str]], done,
            secret: str = "") -> None:
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
            if label == secret:
                entry.set_visibility(False)
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
        """Keys, matched by hand rather than by accelerator.

        GtkShortcutController looked like the right tool and quietly is not:
        an accelerator asking for CTRL+SHIFT+c never fires, because shift is
        consumed producing the C the event actually carries, and what is left
        no longer matches what was asked for. Nothing errors -- the shortcut
        simply does nothing, which is how ctrl-shift-c, -v, -w and -k were all
        dead without anyone noticing.

        A key controller hands over the keyval and the modifiers and lets this
        decide. It is longer, and it is readable, and it can be tested without
        a keyboard.

        CAPTURE phase because a focused VTE swallows nearly everything: alt-1
        it would forward to the shell as ESC-1.
        """
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.key_pressed)
        self.add_controller(keys)

    def key_pressed(self, _controller, keyval, _code, state) -> bool:
        """True means helm took the key; False leaves it to the terminal."""
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        # Lowered, because shift has already turned c into C by the time this
        # sees it -- which is the whole reason accelerators did not work.
        name = (Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or "").lower()

        if alt and not ctrl and name.isdigit() and name != "0":
            return self.show_nth(int(name) - 1)

        if ctrl and name in ("tab", "iso_left_tab"):
            return self.cycle(-1 if shift else 1)
        if ctrl and name == "page_down":
            return self.cycle(1)
        if ctrl and name == "page_up":
            return self.cycle(-1)

        if name == "f12":
            return self.focus_list()
        if name == "f11":
            return self.toggle_fullscreen()

        if ctrl and not shift:
            if name == "f":
                self.filter.grab_focus()
                return True
            if name == "q":
                self.close()
                return True

        if ctrl and shift:
            if name == "c":
                return self.on_terminal("copy")
            if name == "v":
                return self.on_terminal("paste")
            if name == "a":
                return self.on_terminal("select_all")
            if name == "w":
                return self.close_visible()
            if name == "k":
                return self.kill_selected()

        return False        # everything else belongs to the terminal

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

    def on_terminal(self, action: str) -> bool:
        """Run one of the terminal's own actions on whichever is showing."""
        current = self.stack.get_visible_child()
        if isinstance(current, Session):
            if action == "select_all":
                current.term.select_all()
            else:
                getattr(current, action)()
        return True

    def kill_selected(self) -> bool:
        """Kill whatever the list is pointing at. Not bound to Delete: this
        runs in the capture phase, so it would be taken from a terminal you
        were typing in."""
        key = self.selected_key()
        if key:
            self.confirm_kill(*key)
        return True

    def toggle_fullscreen(self) -> bool:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()
        return True

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

    def _wordmark(self) -> Gtk.Widget:
        """The name, drawn rather than written.

        A list of servers is a utility; the thing you look at all day may as
        well say what it is. It doubles as the count of what is waiting, which
        is the one number worth reading from across the room.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=14, margin_bottom=10,
                      margin_start=14, margin_end=14)

        top = Gtk.Box(spacing=10)
        wheel = Gtk.Label(label="\u2388")
        wheel.add_css_class("wheel")
        wheel.set_valign(Gtk.Align.CENTER)
        top.append(wheel)

        mark = Gtk.Label(label=WORDMARK, xalign=0)
        mark.add_css_class("wordmark")
        top.append(mark)
        box.append(top)

        self.subtitle = Gtk.Label(label="", xalign=0)
        self.subtitle.add_css_class("tagline")
        box.append(self.subtitle)

        actions = Gtk.Box(spacing=4, margin_top=8)
        for icon, tip, handler in (
            ("list-add-symbolic", "New session on the selected host",
             lambda: self.prompt_new_session()),
            ("network-server-symbolic", "Add a server to ~/.ssh/config",
             lambda: self.prompt_add_host()),
            ("view-refresh-symbolic", "Refresh every host now",
             lambda: self.load_hosts()),
        ):
            button = Gtk.Button(icon_name=icon)
            button.add_css_class("action")
            button.set_tooltip_text(tip)
            button.connect("clicked", lambda _b, h=handler: h())
            actions.append(button)
        box.append(actions)
        return box

    def _footer(self) -> Gtk.Widget:
        """Help sits at the bottom of the list, where it is out of the way
        until the moment you want it."""
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=8,
                      margin_start=10, margin_end=10)
        bar.add_css_class("footer")

        help_button = Gtk.MenuButton(label="?")
        help_button.add_css_class("help")
        help_button.set_tooltip_text("What the marks mean, the keys, and who wrote it")
        help_button.set_popover(self._guide())
        bar.append(help_button)

        self.footnote = Gtk.Label(label="", xalign=0)
        self.footnote.add_css_class("detail")
        self.footnote.set_hexpand(True)
        bar.append(self.footnote)

        # Shown only when there is nothing to unlock it with. A padlock that
        # is always there stops being read.
        self.unlock = Gtk.Button(label="unlock key")
        self.unlock.add_css_class("locked")
        self.unlock.set_tooltip_text(
            "The ssh agent is holding no keys, so every host will refuse you. "
            "This runs ssh-add in a terminal -- the passphrase goes to it, "
            "never through helm.")
        self.unlock.set_visible(False)
        self.unlock.connect("clicked", lambda _b: self.do_unlock())
        bar.append(self.unlock)

        # The one thing genuinely lost with the title bar. Down here it is
        # still there fullscreen, which is where it went missing before.
        shut = Gtk.Button(icon_name="window-close-symbolic")
        shut.add_css_class("action")
        shut.set_tooltip_text("Close helm (ctrl-q). Sessions keep running.")
        shut.connect("clicked", lambda _b: self.close())
        bar.append(shut)
        return bar

    def do_unlock(self) -> None:
        hosts.unlock_agent()
        # The terminal outlives this call, so the state is re-read shortly
        # after rather than now, when it would still say locked.
        GLib.timeout_add_seconds(6, lambda: (self.check_agent(), False)[1])

    def check_agent(self) -> None:
        def look():
            keys = hosts.agent_keys()
            GLib.idle_add(self.show_agent, keys)
        threading.Thread(target=look, daemon=True).start()

    def show_agent(self, keys) -> bool:
        """None is no agent at all, 0 is an agent holding nothing. Both mean
        key authentication is going to fail, which is worth saying before
        seven hosts turn red for no visible reason."""
        locked = not keys
        self.unlock.set_visible(locked)
        if locked:
            self.unlock.set_label(
                "no ssh agent" if keys is None else "unlock key")
        return GLib.SOURCE_REMOVE

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
        .side {{ background: {p.panel};
                 border-right: 1px solid {p.surface}; }}
        .sidebar {{ background: transparent; }}
        .wheel {{ color: {p.accent}; font-size: 1.9em; }}
        .wordmark {{ color: {p.accent}; font-family: monospace;
                     font-size: 0.78em; line-height: 1.0; }}
        .tagline {{ color: {p.muted}; font-size: 0.85em; padding-top: 4px; }}
        .tagline.waiting {{ color: {p.red}; }}
        .footer {{ border-top: 1px solid {p.surface}; }}
        .help {{ font-family: monospace; font-weight: bold;
                 color: {p.accent}; min-width: 26px; }}
        .link {{ color: {p.accent}; font-size: 0.85em; padding: 0; }}
        .filter {{ margin: 0 12px 6px 12px; font-size: 0.9em; }}
        .locked {{ color: {p.red}; font-size: 0.8em; padding: 2px 8px; }}
        .action {{ min-width: 26px; min-height: 26px; padding: 2px; }}
        .secret {{ font-family: monospace; }}
        .sidebar row {{ padding: 2px 10px; }}
        .sidebar row:selected {{ background: {p.accent}; color: {p.background}; }}
        /* Child labels carry their own colour, and one of them is the accent
           itself -- which on the accent-coloured selection is invisible. */
        .sidebar row:selected .slot,
        .sidebar row:selected .slot-open,
        .sidebar row:selected .name,
        .sidebar row:selected .detail {{ color: {p.background}; }}
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
        .kill {{ color: {p.red}; opacity: 0; min-width: 22px; min-height: 22px;
                 padding: 0; margin: 0; }}
        .sidebar row:hover .kill {{ opacity: 0.75; }}
        .sidebar row:hover .kill:hover {{ opacity: 1; }}
        .sidebar row:selected .kill {{ color: {p.background}; opacity: 0.7; }}
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
        # The one number worth reading from across the room, so it is allowed
        # to be the one coloured thing up there.
        if waiting:
            self.subtitle.add_css_class("waiting")
        else:
            self.subtitle.remove_css_class("waiting")
        live = sum(1 for host in self.order if self.rows[host]["state"] == "up")
        self.footnote.set_text(
            f"{live}/{len(self.order)} hosts  ·  {len(slots)} sessions")

        self.announce()

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

    def announce(self) -> None:
        """Say, once, when a session starts waiting on you.

        The panel is honest about who is blocked, but only to someone looking
        at it -- and the thing worth knowing is precisely that a chat has been
        sitting there while you did something else. So it says so out loud on
        the edge into needs-you, and not again until it has been something
        else in between.

        The first sweep only records: everything already waiting when helm
        opens is on screen, and five notifications for it would be noise.

        A blocked chat is worth saying the moment it happens. A draft is not,
        which is what draft_settled() is for.
        """
        first = not self.was
        now = time.monotonic()
        for host in self.order:
            for session in self.rows[host]["sessions"]:
                key = (host, session["name"])
                state = session["agent"]["state"]
                before = self.was.get(key)
                self.was[key] = state
                fresh = first or before is None

                if state == agent_state.DRAFT:
                    if self.draft_settled(key, session["agent"]["detail"],
                                          now, silent=fresh):
                        self.notify(host, session)
                    continue

                self.drafts.pop(key, None)
                if fresh or before == state or state != agent_state.NEEDS_YOU:
                    continue
                self.notify(host, session)

        for gone in [k for k in self.was
                     if k not in {(h, s["name"]) for h in self.order
                                  for s in self.rows[h]["sessions"]}]:
            del self.was[gone]
            self.drafts.pop(gone, None)

    def draft_settled(self, key: tuple[str, str], text: str, now: float,
                      silent: bool) -> bool:
        """Whether an unsent draft has sat untouched long enough to be worth
        interrupting for -- and only the first time it has.

        Typing is a draft too. The box holds text from the first keystroke on,
        so announcing the state itself fired a notification at whoever was at
        the keyboard, about the sentence they were in the middle of. What
        separates the draft worth knowing about is that it stops changing:
        every edit puts the clock back to the start, and the toast is for the
        one that then just sits there.

        Only the first 60 characters of the box reach here, so a draft still
        being typed past that mark can look settled. Two minutes is a long
        time to spend on one sentence, and one toast is the whole cost.
        """
        seen, since, told = self.drafts.get(key, (None, now, silent))
        if text != seen:
            seen, since, told = text, now, silent
        settled = not told and now - since >= DRAFT_DWELL
        self.drafts[key] = (text, since, told or settled)
        return settled

    def notify(self, host: str, session: dict) -> None:
        note = Gio.Notification.new(f"{host}/{session['name']}")
        detail = session["agent"]["detail"]
        note.set_body(f"{session['agent']['label']}"
                      + (f" -- {detail}" if detail else ""))
        note.set_priority(Gio.NotificationPriority.NORMAL)
        # Clicking it opens the session it is about, which is the only thing
        # anyone wants from a notification like this.
        note.set_default_action_and_target(
            "app.open-session",
            GLib.Variant("s", f"{host}\t{session['name']}"))
        self.get_application().send_notification(
            f"helm-{host}-{session['name']}", note)

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

    def matches(self, row) -> bool:
        """Filter on what is written on the row.

        A host that matches keeps its sessions, or filtering by host name
        would hide everything on it -- which is the opposite of what typing a
        host name means.
        """
        needle = self.filter.get_text().strip().lower()
        if not needle:
            return True
        key = getattr(row, "key", None)
        if key is None:
            return needle in getattr(row, "host", "").lower()
        return needle in key[0].lower() or needle in key[1].lower()

    def clear_filter(self) -> None:
        self.filter.set_text("")
        self.list.invalidate_filter()
        self.list.grab_focus()

    def _host_row(self, host: str, data: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        # A host is a heading, not a destination: there is nothing to open on
        # it, so it neither highlights nor answers a click.
        row.key = None
        row.host = host          # so the filter can read it off the row
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
        menu = Gtk.GestureClick()
        menu.set_button(3)
        menu.connect("pressed",
                     lambda _g, _n, x, y, r=row, h=host: self.host_menu(r, h, x, y))
        row.add_controller(menu)

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

        # Right-click is not a feature anyone finds. The same kill, on the
        # row, shown when the pointer is over it so eight of them are not
        # sitting there inviting a misclick.
        kill = Gtk.Button(icon_name="user-trash-symbolic")
        kill.add_css_class("kill")
        kill.set_has_frame(False)
        kill.set_tooltip_text(
            f"Kill {host}/{session['name']} -- everything running in it ends")
        kill.connect("clicked",
                     lambda _b, h=host, n=session["name"]: self.confirm_kill(h, n))
        box.append(kill)

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
        """Open one, or come back to it if it is already open.

        Both ends go through show(), so the list always agrees with the
        terminal about which session you are looking at -- opening one by its
        number used to move the terminal and leave the highlight behind.
        """
        key = (host, name)
        if key in self.open:
            self.show(self.open[key])
            return

        session = Session(host, name, self.palette, self.close_session,
                          screen=lambda h=host, n=name: self.screen_of(h, n))
        self.open[key] = session
        self.stack.add_named(session, f"{host}/{name}")
        self.show(session)
        # The number beside it goes accent once it is open, and that is drawn
        # by the list rather than by the stack.
        self.repaint()

    def screen_of(self, host: str, name: str) -> str:
        """The last screen the watcher brought back for one session."""
        for session in self.rows.get(host, {}).get("sessions", []):
            if session["name"] == name:
                return session.get("screen", "")
        return ""

    def close_session(self, session: Session) -> None:
        """A session whose command ended takes its terminal with it.

        The tmux session on the other side is untouched -- that is the whole
        point of it -- so this is closing a view, not ending any work.
        """
        self.open.pop((session.host, session.name), None)
        self.stack.remove(session)
        self.repaint()
        if self.open:
            # Which one to fall back to is ours to choose: left alone, the
            # stack picks whatever child it likes -- usually the placeholder,
            # while two sessions are still open behind it.
            self.show(list(self.open.values())[-1])
        else:
            self.stack.set_visible_child(self.placeholder)
            # The highlight stays where it was, as the cursor for the arrow
            # keys. Nothing is showing, and nothing pretends to be.

    # -- probing -----------------------------------------------------------

    def sweep_tick(self) -> bool:
        self.sweep()
        self.check_agent()
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
                    data["screen"], data["commands"], data.get("raw", ""))
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

        # Clicking a notification lands here, whether or not the window was
        # the thing you were looking at.
        action = Gio.SimpleAction.new("open-session", GLib.VariantType("s"))
        action.connect("activate", self.open_from_notification)
        self.add_action(action)

    def open_from_notification(self, _action, target):
        window = self.props.active_window
        if window is None:
            return
        host, _, name = target.get_string().partition("\t")
        window.present()
        window.open_session(host, name)

    def do_activate(self):
        window = self.props.active_window or Helm(self)
        window.present()


def run() -> int:
    return HelmApp().run(None)
