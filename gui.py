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
            None, None,
            -1,
            None,
            None, None,
        )

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
        self.watched: set[str] = set()
        self.watchers: dict[str, object] = {}
        self.inflight: set[str] = set()
        self.frames: queue.Queue = queue.Queue(maxsize=256)
        self.stopping = False

        self._build()
        self._style()

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

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh every host now")
        refresh.connect("clicked", lambda _b: self.load_hosts())
        header.pack_end(refresh)
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
                  "It opens here -- this window keeps the list.")
        self.placeholder.add_css_class("placeholder")
        self.placeholder.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.placeholder, "placeholder")

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_start_child(side)
        split.set_end_child(self.stack)
        split.set_position(300)
        split.set_resize_start_child(False)
        self.set_child(split)

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
        """Rebuild the sidebar, putting the selection back where it was.

        Rebuilding wholesale is honest about what changed -- sessions come and
        go on their own -- and at this size it is cheaper than diffing.
        """
        chosen = None
        row = self.list.get_selected_row()
        if row is not None:
            chosen = getattr(row, "key", None)

        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)

        waiting = 0
        for host in self.order:
            data = self.rows[host]
            self.list.append(self._host_row(host, data))
            for session in data["sessions"]:
                state = session["agent"]["state"]
                if state in (agent_state.NEEDS_YOU, agent_state.DRAFT):
                    waiting += 1
                self.list.append(self._session_row(host, session))

        self.subtitle.set_text(
            f"{waiting} waiting on you" if waiting else "nothing waiting")

        if chosen is not None:
            index = 0
            while (row := self.list.get_row_at_index(index)) is not None:
                if getattr(row, "key", None) == chosen:
                    self.list.select_row(row)
                    break
                index += 1

    def _host_row(self, host: str, data: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.key = None                      # not activatable: nothing to open
        row.set_activatable(False)
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

    def _session_row(self, host: str, session: dict) -> Gtk.ListBoxRow:
        state = session["agent"]["state"]
        row = Gtk.ListBoxRow()
        row.key = (host, session["name"])

        box = Gtk.Box(spacing=8)
        mark = Gtk.Label(label=MARKS.get(state, "?"))
        mark.add_css_class("mark")
        mark.set_size_request(12, -1)
        colour = mark_colour(self.palette, state)
        mark.set_attributes(self._colour_attrs(colour))
        box.append(mark)

        name = Gtk.Label(label=session["name"], xalign=0)
        name.add_css_class("name")
        box.append(name)

        detail = session["agent"]["label"]
        if session["agent"]["detail"]:
            detail = f"{detail}  {session['agent']['detail']}"
        tail = Gtk.Label(label=detail, xalign=1)
        tail.add_css_class("detail")
        tail.set_hexpand(True)
        tail.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(tail)

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
