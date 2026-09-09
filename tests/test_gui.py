"""What the window does, checked without a pair of hands.

Every bug this file covers was found by hand first, which is the argument for
it: switching that moved the terminal and left the list behind, a menu whose
parent was rebuilt out from under it, controls kept somewhere fullscreen takes
away. None of those are visible to a test of hosts.py, and all of them are one
assertion each here.

Needs a display, because GTK needs one -- it skips rather than fails without.
Real tmux sessions are created on this machine and killed again at the end.
The run is pointed at an ssh config, star file, zoom file and mount root of
its own before the window is built, so nothing remote is touched and nothing
of yours is rewritten. The one check against real user state -- the keyring
round trip -- only runs with CONN_TEST_KEYRING=1, and clears what it stored
either way.

    python3 tests/test_gui.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONN_NO_WATCH", "1")

SESSIONS = ("conntest-alpha", "conntest-beta")
# Made by the window itself, the way you make one from the + button:
# it exists before anything has told the list about it.
NEW_SESSION = "conntest-gamma"


def display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def tmux(*args: str) -> str:
    done = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return done.stdout


def sessions_now() -> list[str]:
    return tmux("list-sessions", "-F", "#{session_name}").split()


class Checks:
    def __init__(self):
        self.failures = 0

    def __call__(self, name: str, passed: bool, detail: str = "") -> None:
        self.failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {name}"
              + (f"   {detail}" if detail and not passed else ""))


def walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def main() -> int:
    if not display():
        print("skipped: no display, and GTK needs one")
        return 0

    import tempfile

    # The window probes every host its config lists and writes stars and zoom
    # where the module constants point, so the fences go up before anything
    # is imported or built: a config of our own -- only "local" is listed,
    # so no ssh ever leaves this machine -- state files of our own, and a
    # mount root of our own for the tidy_mounts() run at construction.
    keep = tempfile.TemporaryDirectory(prefix="conn-test-",
                                       ignore_cleanup_errors=True)
    state = Path(keep.name)
    (state / "config").write_text("")
    os.environ["CONN_SSH_CONFIG"] = str(state / "config")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Vte", "3.91")
    from gi.repository import GLib, Gtk

    import agent_state
    import gui
    import hosts

    gui.STARS_STATE = state / "starred"
    gui.ORDER_STATE = state / "order"
    gui.ZOOM_STATE = state / "zoom"
    hosts.MNT_ROOT = state / "mnt"

    check = Checks()
    for name in SESSIONS:
        tmux("new-session", "-d", "-s", name, "bash --norc")

    # The window builds on activate, but its first probe lands on a worker,
    # so the checks wait for its rows to arrive rather than guessing how long
    # that takes. The deadline turns a probe that never returns into failures
    # over the listing that actually arrived, not a hang.
    class Panel(Gtk.Application):
        def do_activate(self):
            window = gui.Conn(self)
            wanted = {("local", name) for name in SESSIONS}
            deadline = time.monotonic() + 30.0

            def settled():
                if not (wanted <= set(window.slots)
                        or time.monotonic() >= deadline):
                    return True
                try:
                    run(window, check, gui, hosts, agent_state, Gtk)
                except Exception:
                    # A crash is a verdict too. PyGObject swallows one thrown
                    # out of a callback, so left uncounted it skipped every
                    # check after it and still printed "all good".
                    check("the checks themselves ran to completion", False,
                          traceback.format_exc())
                finally:
                    try:
                        for session in list(window.open.values()):
                            window.close_session(session)
                    except Exception:
                        check("and cleaned up after themselves", False,
                              traceback.format_exc())
                    self.quit()
                return False

            GLib.timeout_add(200, settled)

    Panel(application_id="org.omarchy.conn.test").run(None)

    for name in (*SESSIONS, NEW_SESSION):
        tmux("kill-session", "-t", name)
    keep.cleanup()

    print(f"\n{'all good' if not check.failures else str(check.failures) + ' failed'}")
    return 1 if check.failures else 0


def run(window, check, gui, hosts, agent_state, Gtk) -> None:
    alpha = ("local", SESSIONS[0])
    beta = ("local", SESSIONS[1])

    # Nothing in here should reach the desktop. A test run that leaves four
    # toasts in your notification centre is a test run you stop wanting to
    # run, and the states below are poked by hand precisely to fire them.
    sent: list = []
    window.notify = lambda host, s: sent.append(
        (host, s["name"], s["agent"]["state"]))

    check("the app tells the desktop what it is called",
          gui.GLib.get_application_name() == "conn",
          f"name={gui.GLib.get_application_name()!r} -- "
          "an unnamed app is announced as python3")

    check("the local host lists its sessions",
          alpha in window.slots and beta in window.slots,
          f"slots={window.slots}")

    # -- switching ---------------------------------------------------------
    window.open_session(*alpha)
    window.open_session(*beta)

    def showing():
        current = window.stack.get_visible_child()
        return (current.host, current.name) if isinstance(current, gui.Session) else None

    def highlighted():
        row = window.list.get_selected_row()
        return getattr(row, "key", None) if row is not None else None

    check("opening one shows it", showing() == beta)
    window.show_nth(window.slots.index(alpha))
    check("alt-N goes to the numbered session", showing() == alpha)
    check("and the list agrees with the terminal", highlighted() == alpha,
          f"showing={showing()} highlighted={highlighted()}")
    window.cycle(1)
    check("ctrl-tab moves and the list follows",
          showing() == highlighted() and showing() != alpha)

    window.close_visible()
    check("closing a view falls back to one that is open",
          isinstance(window.stack.get_visible_child(), gui.Session))
    check("and the highlight follows that too", showing() == highlighted())

    # -- one made before the list has heard of it --------------------------
    # A session from the + button is running in the terminal a second before
    # the probe brings its row in. The highlight cannot go anywhere yet, and
    # the render that brings the row in used to put it back where it was --
    # so the list sat pointing at the session you had just left.
    fresh = ("local", NEW_SESSION)
    window.open_session(*fresh)
    check("a session opens before it is listed", showing() == fresh,
          f"showing={showing()}")
    check("and the highlight is owed to it", window.pending == fresh,
          f"pending={window.pending}")
    listed = window.rows["local"]["sessions"]
    listed.append(dict(listed[0], name=NEW_SESSION,
                       agent=dict(listed[0]["agent"], state=agent_state.READY,
                                  label="idle", detail="")))
    window.render()
    check("and it takes the highlight as soon as its row arrives",
          highlighted() == fresh,
          f"highlighted={highlighted()} showing={showing()}")
    check("which settles the debt", window.pending is None,
          f"pending={window.pending}")

    # The other half of it: the debt is only owed while that session is the
    # one on screen. Go somewhere else first and the highlight stays there.
    listed.pop()
    window.render()
    check("dropping the row leaves the highlight owed again",
          window.pending == fresh, f"pending={window.pending}")
    window.open_session(*alpha)
    listed.append(dict(listed[0], name=NEW_SESSION,
                       agent=dict(listed[0]["agent"], state=agent_state.READY,
                                  label="idle", detail="")))
    window.render()
    check("a session you have left behind does not take the highlight back",
          highlighted() == alpha, f"highlighted={highlighted()}")

    # -- the list under a changing screen ----------------------------------
    row = next((r for r in rows(window) if getattr(r, "key", None) == alpha), None)
    before = id(row)
    session = next(s for s in window.rows["local"]["sessions"]
                   if s["name"] == alpha[1])
    session["agent"] = dict(session["agent"], state=agent_state.NEEDS_YOU,
                            label="needs you", detail="Do you want to")
    window.render()
    row = next((r for r in rows(window) if getattr(r, "key", None) == alpha), None)
    check("a screen changing repaints rather than replaces them",
          id(row) == before,
          "rebuilding rows kills the menu parented to one")
    check("and the words change with it",
          window.widgets[alpha]["mark"].get_label() == "!")

    # -- the condition of the whole fleet, in one bar -----------------------
    # The bar sums over every session on the machine, including whatever you
    # happen to be running right now, so the fleet is quieted first. Without
    # this the check passes or fails depending on what is busy in another
    # window, which is not a property of the code.
    for other in window.order:
        for quiet in window.rows[other]["sessions"]:
            quiet["agent"] = dict(quiet["agent"], state=agent_state.READY,
                                  label="idle", detail="")
    session["agent"] = dict(session["agent"], state=agent_state.NEEDS_YOU,
                            label="needs you", detail="Do you want to")
    window.render()
    check("one session waiting puts the fleet at red alert",
          window.alert == agent_state.NEEDS_YOU, f"alert={window.alert}")
    session["agent"] = dict(session["agent"], state=agent_state.WORKING,
                            label="working", detail="")
    window.render()
    check("nothing waiting but something working is yellow",
          window.alert == agent_state.WORKING, f"alert={window.alert}")
    check("and the line under the name says so too",
          window.subtitle.get_text().endswith("working"),
          f"subtitle={window.subtitle.get_text()!r} -- an amber bar over "
          "\"nothing waiting\" reads as a contradiction")
    session["agent"] = dict(session["agent"], state=agent_state.READY,
                            label="idle", detail="")
    window.render()
    check("and a quiet fleet is all clear",
          window.alert == agent_state.READY, f"alert={window.alert}")
    check("in both places",
          window.subtitle.get_text() == "all clear",
          f"subtitle={window.subtitle.get_text()!r}")

    # -- notifications -----------------------------------------------------
    sent.clear()
    window.render()
    check("no notification while the state stands", not sent)
    session["agent"] = dict(session["agent"], state=agent_state.READY, label="idle")
    window.render()
    session["agent"] = dict(session["agent"], state=agent_state.NEEDS_YOU,
                            label="needs you", detail="Do you want to")
    window.render()
    check("one when it starts waiting on you again",
          [s[2] for s in sent] == [agent_state.NEEDS_YOU], f"sent={sent}")

    # A draft is text you are in the middle of typing until it stops changing,
    # so it is announced on the silence, not on the first keystroke. The clock
    # watches the box, not the detail: the detail carries a background-work
    # suffix that ticks every frame, and a clock keyed on it never settles.
    sent.clear()
    session["agent"] = dict(session["agent"], state=agent_state.DRAFT,
                            label="unsent draft", detail="what's the health",
                            draft="what's the health")
    window.render()
    check("typing is not an interruption", not sent, f"sent={sent}")

    def rewind(by=gui.DRAFT_DWELL + 1):
        text, since, told = window.drafts[alpha]
        window.drafts[alpha] = (text, since - by, told)

    session["agent"] = dict(session["agent"], draft="what's the health check")
    rewind()
    window.render()
    check("and an edit puts the clock back", not sent, f"sent={sent}")

    # Background work ticking in the detail is not an edit -- the box is
    # unchanged, so the clock keeps running and the settled draft is said.
    session["agent"] = dict(session["agent"],
                            detail="what's the health check  (running tests)")
    rewind()
    window.render()
    check("a draft left sitting is worth saying",
          [s[2] for s in sent] == [agent_state.DRAFT], f"sent={sent}")
    rewind()
    window.render()
    check("but only the once", len(sent) == 1, f"sent={sent}")

    # -- filter ------------------------------------------------------------
    window.filter.set_text(SESSIONS[0])
    check("filtering keeps what matches",
          window.matches(row_for(window, alpha)))
    check("and drops what does not",
          not window.matches(row_for(window, beta)))
    window.filter.set_text("local")
    check("a host name keeps the sessions under it",
          window.matches(row_for(window, alpha)))
    window.clear_filter()

    # -- controls that must outlive fullscreen -----------------------------
    icons = [c.get_icon_name() for c in walk(window.get_child().get_first_child())
             if isinstance(c, Gtk.Button) and c.get_icon_name()]
    check("every action lives in the sidebar",
          {"list-add-symbolic", "network-server-symbolic",
           "view-refresh-symbolic", "window-close-symbolic"} <= set(icons),
          f"icons={icons}")
    # -- one window, however the app is reached -----------------------------
    # A notification click arrives while conn is unfocused, which is exactly
    # when GtkApplication reports no active window at all. Reaching for that
    # built a second window every time, presented it, and opened the session
    # in the one you were not looking at.
    app = window.get_application()
    check("the app reuses the window it already has",
          gui.ConnApp.window(app) is window,
          "a second window is what a notification click used to open")

    # -- the sidebar has to survive its own footer -------------------------
    side = window.get_child().get_first_child()
    window.updated.set_visible(True)
    window.unlock.set_visible(True)
    smallest, _ = side.get_preferred_size()
    check("the sidebar still fits when the footer buttons appear",
          smallest.width <= gui.SIDEBAR_WIDTH,
          f"minimum={smallest.width}px against a {gui.SIDEBAR_WIDTH}px "
          "sidebar -- one that cannot fit its own minimum is drawn clipped")
    check("and the list cannot be resized by accident",
          not isinstance(window.get_child(), Gtk.Paned),
          "a drag handle beside a list you click all day is a handle you "
          "catch by mistake")

    # Ellipsizing the names stopped a long one widening the sidebar, but it
    # also let a squeezed sidebar shrink "local" to "lo..."; a width-chars
    # floor is what puts the readable minimum back.
    host_row = next(r for r in rows(window)
                    if getattr(r, "host", "") and getattr(r, "key", None) is None)
    host_label = host_row.get_child().get_first_child()
    check("a host name keeps a width-chars floor, so it cannot shrink to 'lo...'",
          host_label.get_width_chars() == gui.NAME_FLOOR
          and int(host_label.get_ellipsize()) != 0,
          f"width_chars={host_label.get_width_chars()} "
          f"ellipsize={int(host_label.get_ellipsize())}")
    least, _ = host_row.get_preferred_size()
    check("so the row will not collapse a short name when the sidebar is squeezed",
          least.width >= 60,
          f"row minimum={least.width}px -- an ellipsis-only floor is ~17px")

    window.updated.set_visible(False)
    window.unlock.set_visible(False)

    check("there is no title bar to lose them with",
          window.get_titlebar() is None and not window.get_decorated())

    # -- the key guide, which is only useful if it is complete -------------
    guide = " ".join(w.get_label() or "" for w in walk(
        window.help_button.get_popover().get_child())
        if isinstance(w, Gtk.Label)).lower()
    # Add the row when you add the key: a guide that lists most of them reads
    # as the whole set, and the ones left out are the ones nobody finds.
    for wanted in ("alt-1..9", "ctrl-tab", "f12", "ctrl-shift-c / v",
                   "ctrl-shift-a", "ctrl-shift-w", "ctrl-shift-k", "ctrl-f",
                   "ctrl-+ - 0", "ctrl-shift-r", "ctrl-shift-s",
                   "f11 / ctrl-q", "f1 or ?",
                   "ctrl-click", "right-click a session", "right-click a host",
                   "right-click the screen", "hover a session"):
        check(f"the guide has {wanted}", wanted in guide,
              "a key nobody can find is a key that does not exist")
    _, natural = window.help_button.get_popover().get_child().get_preferred_size()
    check("and fits on a laptop screen, popovers being clipped and not scrolled",
          natural.height <= 600, f"height={natural.height}")
    check("and says what every mark means",
          all(word in guide for word in ("needs you", "unsent draft", "working",
                                         "idle", "shell", "unknown")))

    # -- font, and the size you set it to ----------------------------------
    import tempfile
    import theming

    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "foot.ini"
        conf.write_text("[main]\n# a comment\n"
                        "font=JetBrainsMono Nerd Font:size=9, Noto Emoji:size=9\n")
        # "sh" stands in for the terminal's binary: what is being checked is
        # that a config is only read when that terminal is actually installed.
        found = theming.terminal_font([("sh", conf, theming._foot_font)])
        check("the session font comes from this machine's terminal",
              found == "JetBrainsMono Nerd Font 9", f"font={found}")
        check("a terminal that is not installed is not consulted",
              theming.terminal_font([("no-such-terminal-xyz", conf,
                                      theming._foot_font)]) == theming.FALLBACK_FONT)

        os.environ["CONN_FONT"] = "Fira Code 12"
        check("and CONN_FONT wins over both",
              theming.terminal_font([("sh", conf, theming._foot_font)])
              == "Fira Code 12")
        del os.environ["CONN_FONT"]

        was_zoom, gui.ZOOM_STATE = gui.ZOOM_STATE, Path(tmp) / "zoom"
        try:
            window.set_zoom(1.3)
            scales = [s.term.get_font_scale() for s in window.open.values()]
            check("zooming resizes every open session", scales and
                  all(abs(scale - 1.3) < 0.001 for scale in scales), f"scales={scales}")
            check("and it survives the next run", abs(gui.read_zoom() - 1.3) < 0.001,
                  f"read={gui.read_zoom()}")
            window.set_zoom(99.0)
            check("nothing can be zoomed off the screen", window.zoom == gui.ZOOM_MAX,
                  f"zoom={window.zoom}")
            window.set_zoom(1.0)
        finally:
            gui.ZOOM_STATE = was_zoom

    # -- killing -----------------------------------------------------------
    kills = [c for c in walk(row_for(window, alpha))
             if isinstance(c, Gtk.Button) and c.has_css_class("kill")]
    check("each session carries its own kill", len(kills) == 1)

    # -- copying out of a session that has the mouse ------------------------
    session_view = window.open[beta] if beta in window.open else None
    if session_view is None:
        window.open_session(*beta)
        session_view = window.open[beta]
    session_view.menu(0.0, 0.0)
    menu = [c for c in walk(session_view.term) if isinstance(c, Gtk.Popover)]
    copy_labels = [b.get_label() for b in walk(menu[-1])
                   if isinstance(b, Gtk.Button)] if menu else []
    for pop in menu:
        pop.popdown()
    check("with nothing selected, copy says how to select",
          any("hold shift" in (label or "") for label in copy_labels),
          f"menu={copy_labels}")
    check("and the whole screen can be taken without selecting at all",
          any("whole screen" in (label or "") for label in copy_labels),
          f"menu={copy_labels}")

    window.show(session_view)
    session_view.term.unselect_all()
    window.on_terminal("copy")
    check("copying nothing says so, rather than looking like a dead key",
          "nothing selected" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")
    window.copy_screen()
    check("and copying the screen reports what it took",
          "screen" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")
    window.said = ("", 0.0)
    window.render()
    check("the footer goes back to the host count afterwards",
          "hosts" in window.footnote.get_text(),
          f"footer={window.footnote.get_text()!r}")

    # -- noticing it has been replaced on disk ------------------------------
    window.check_source()
    check("no restart nag while this is the installed version",
          not window.updated.get_visible())
    # As if an install had landed with cp -p: the mtimes it brings are the
    # source's own, so nothing on disk is newer -- but a file's size changed.
    path, (size, mtime) = next(iter(window.stamp.items()))
    window.stamp = {**window.stamp, path: (size + 1, mtime)}
    window.check_source()
    check("and one as soon as a different one is copied over it",
          window.updated.get_visible(),
          "three times running, a feature was missing only because the "
          "window was still the build from yesterday")
    window.stamp = gui.source_stamp()
    window.check_source()

    # -- the menus, which is where most of this is actually reached --------
    def menu_items(build, *args):
        """What a right-click offers. The popover is parented to the list, so
        it is still there to read after the call returns."""
        build(*args)
        popover = [c for c in walk(window.list) if isinstance(c, Gtk.Popover)]
        labels = [b.get_label() for b in walk(popover[-1])
                  if isinstance(b, Gtk.Button)] if popover else []
        for pop in popover:
            pop.popdown()
        return labels

    on_session = menu_items(window.row_menu, row_for(window, alpha), 0, 0)
    check("right-clicking a session offers to rename it",
          any("Rename" in (label or "") for label in on_session),
          f"menu={on_session}")
    check("as well as open and kill",
          any("Open" in (label or "") for label in on_session)
          and any("Kill" in (label or "") for label in on_session),
          f"menu={on_session}")
    on_host = menu_items(window.host_menu, row_for(window, alpha), "local", 0, 0)
    check("and a host offers a new session and its passwords",
          any("New session" in (label or "") for label in on_host)
          and any("Passwords" in (label or "") for label in on_host),
          f"menu={on_host}")
    check("and a way to check it again, for one that came back down",
          any("Check again" in (label or "") for label in on_host),
          f"menu={on_host}")
    # "local" is invented when the config has no such entry, so there is no
    # line to take out and the menu must not offer to.
    check("and no offer to forget a host the config never named",
          any("Forget" in (label or "") for label in on_host)
          == ("local" in hosts.config_hosts()),
          f"menu={on_host}")
    # A host the config does name is offered for forgetting further down,
    # beside the forget flow itself, against a config of this run's own.

    # -- the new-session dialog names the host it is about ----------------
    def dialog_labels():
        """Every label in the modal that is currently up, with its classes."""
        tops = Gtk.Window.get_toplevels()
        found = []
        for i in range(tops.get_n_items()):
            top = tops.get_item(i)
            if top is window or not top.get_visible():
                continue
            for child in walk(top):
                if isinstance(child, Gtk.Label):
                    found.append((child.get_text(), child.get_css_classes()))
        return found, [tops.get_item(i) for i in range(tops.get_n_items())
                       if tops.get_item(i) is not window]

    window.prompt_new_session("local")
    labels, opened = dialog_labels()
    check("the new-session dialog says which host in its body",
          any(text == "local" for text, _ in labels),
          f"labels={[t for t, _ in labels]}")
    check("and sets it apart from the words around it",
          any(text == "local" and "heading" in classes
              for text, classes in labels),
          f"labels={labels}")
    for top in opened:
        top.destroy()

    # -- the ssh waits for a key rather than asking for one per connection --
    # Driven through show_agent directly: what matters is the decision, and
    # the real agent on the machine running this is unlocked either way.
    released = []
    real_connect = window.connect_hosts
    window.connect_hosts = lambda: (released.append(True),
                                    setattr(window, "holding", False))[0]
    window.holding = True
    window.show_agent(0)
    check("an agent holding nothing holds the connections back",
          window.holding and not released,
          f"holding={window.holding} released={released}")
    window.show_agent(None)
    check("and so does no agent at all",
          window.holding and not released,
          f"holding={window.holding} released={released}")
    window.show_agent(2)
    check("a key in it lets them go, once",
          released == [True] and not window.holding,
          f"holding={window.holding} released={released}")
    window.show_agent(2)
    check("and not again on the next sweep", released == [True],
          f"released={released}")
    window.connect_hosts = real_connect

    # -- renaming, which must not disturb what is running ------------------
    renamed = hosts.rename_session("local", alpha[1], alpha[1] + "-renamed")
    check("a session can be renamed", renamed == alpha[1] + "-renamed",
          f"got={renamed}")
    check("and tmux agrees", renamed in sessions_now(), f"left={sessions_now()}")
    was_open = window.open.get(alpha)
    window.renamed("local", alpha[1], renamed)
    check("the open view follows it",
          ("local", renamed) in window.open and alpha not in window.open,
          f"open={list(window.open)}")
    check("and it is the same terminal, not a new one",
          was_open is not None and window.open[("local", renamed)] is was_open)
    check("which now knows its own name",
          window.open[("local", renamed)].name == renamed)
    check("a name tmux cannot take is filtered, not refused",
          hosts.SESSION_NAME.sub("_", "two words.here") == "two_words_here")
    taken = None
    try:
        hosts.rename_session("local", renamed, beta[1])
    except hosts.HostError as exc:
        taken = str(exc)
    check("a name already in use comes back as tmux said it",
          taken and "duplicate" in taken, f"error={taken}")
    alpha = ("local", renamed)

    hosts.kill_session(*alpha)
    left = sessions_now()
    check("which ends that session", alpha[1] not in left, f"left={left}")
    check("and leaves the one beside it", beta[1] in left, f"left={left}")

    # -- links, including the ones tmux delivered in pieces -----------------
    wrapped = ("  Browser didn't open? Use the url below to sign in\n"
               "\n"
               "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a\n"
               "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com\n"
               "&state=R5GTtVz03mLeyVZRzhDoEcK\n"
               "\n"
               "  Waiting, or press (c) to copy\n")
    found = hosts.screen_links(wrapped)
    check("a URL wrapped across rows comes back whole",
          found and found[0].endswith("R5GTtVz03mLeyVZRzhDoEcK") and len(found[0]) > 120,
          f"found={found}")
    check("and only that one", len(found) == 1, f"found={found}")
    prose = "see https://example.com/x, and (https://example.com/y) too\nplain text\n"
    check("ordinary links still come out one each",
          hosts.screen_links(prose) == ["https://example.com/x", "https://example.com/y"],
          f"found={hosts.screen_links(prose)}")
    check("two full-width lines are not glued into one",
          hosts.screen_links("a" * 130 + "\n" + "b" * 130 + "\n") == [])

    # -- forgetting a server ------------------------------------------------
    # Against a config of our own: the real one is not a fixture, and this
    # rewrites the file it is pointed at.
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        conf.write_text("Host alpha\n    HostName a.example\n"
                        "\n"
                        "Host beta gamma\n    HostName b.example\n"
                        "\n"
                        "# The defaults, which must stay last\n"
                        "Host *\n    ServerAliveInterval 30\n")
        was, hosts.SSH_CONFIG = hosts.SSH_CONFIG, conf
        try:
            backup = hosts.remove_host("alpha")
            left = conf.read_text()
            check("forgetting a server takes its whole block",
                  "alpha" not in left and "a.example" not in left, left)
            check("and keeps the old file beside it", backup.exists())
            check("and leaves every other block alone",
                  "Host beta gamma" in left and "Host *" in left, left)
            check("and does not eat the comment introducing the next block",
                  "# The defaults" in left, left)

            hosts.remove_host("gamma")
            left = conf.read_text()
            check("a name sharing a Host line is dropped from the line, "
                  "not the block",
                  "Host beta\n" in left and "b.example" in left, left)

            try:
                hosts.remove_host("nope")
                said = False
            except hosts.HostError:
                said = True
            check("and forgetting one that was never there says so", said)
        finally:
            hosts.SSH_CONFIG = was

    # -- starring: the few you actually work on -----------------------------
    # Against a state file of our own, or the run would rewrite yours.
    with _tempfile.TemporaryDirectory() as tmp:
        was, gui.STARS_STATE = gui.STARS_STATE, Path(tmp) / "starred"
        try:
            window.load_hosts(connect=False)
            check("a first run stars every server you already had",
                  window.starred == set(window.order),
                  f"starred={window.starred}")
            check("and writes that down", gui.STARS_STATE.exists())

            live = next(s for s in window.rows["local"]["sessions"]
                        if s["name"] == beta[1])
            live["agent"] = dict(live["agent"], state=agent_state.NEEDS_YOU,
                                 label="needs you", detail="")
            window.toggle_star("local")
            check("unstarring takes a server out of the top list",
                  "local" not in window.starred, f"starred={window.starred}")
            check("and its sessions go with it",
                  row_for(window, beta) is None)
            check("but it is still counted -- unstarred is quieter, "
                  "not unwatched",
                  window.alert == agent_state.NEEDS_YOU,
                  f"alert={window.alert}")

            rest = [r for r in rows(window) if getattr(r, "rest", False)]
            check("one row stands in for everything unstarred", len(rest) == 1,
                  f"rest rows={len(rest)}")
            check("and the filter does not turn that row up",
                  not window.matches(rest[0]) if window.filter.get_text()
                  else True)

            window.row_activated(window.list, rest[0])
            check("clicking it brings them back",
                  row_for(window, beta) is not None)
            again = [r for r in rows(window) if getattr(r, "rest", False)]
            window.row_activated(window.list, again[0])
            check("and clicking it again folds them away",
                  row_for(window, beta) is None)

            window.filter.set_text(beta[1])
            window.refilter()       # GTK debounces search-changed; this is it
            check("a search turns up an unstarred server anyway",
                  row_for(window, beta) is not None,
                  "hiding what you are searching for is the one moment "
                  "this arrangement would be wrong")
            window.clear_filter()

            window.toggle_star("local")
            check("starring it again puts it back at the top",
                  "local" in window.starred
                  and row_for(window, beta) is not None)
        finally:
            gui.STARS_STATE = was
            window.load_hosts(connect=False)

    # -- reordering the servers --------------------------------------------
    # arrange() and move_within_tier() are pure, so check the awkward cases
    # directly, then drive one move through the window against a config, order
    # file and stars of this run's own.
    check("arrange leads with the saved order, the rest behind in config order",
          gui.arrange(["local", "a", "b", "c"], ["c", "a"])
          == ["c", "a", "local", "b"])
    check("arrange drops a saved name whose host is gone",
          gui.arrange(["local", "a"], ["x", "a"]) == ["a", "local"])
    check("arrange tolerates a duplicate in a hand-edited order file",
          gui.arrange(["local", "a", "b"], ["b", "b", "a"])
          == ["b", "a", "local"])
    check("a move swaps with the neighbour in the same tier",
          gui.move_within_tier(["a", "b", "c"], {"a", "b", "c"}, "b", -1)
          == ["b", "a", "c"])
    check("and never crosses the fold into the unstarred",
          gui.move_within_tier(["a", "b", "c"], {"a", "b"}, "b", 1) is None,
          "b is last among the starred; c is unstarred and folded away")
    check("the top of a tier has nowhere to move up to",
          gui.move_within_tier(["a", "b"], {"a", "b"}, "a", -1) is None)

    with _tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        conf.write_text("Host alpha\n    HostName a\n\n"
                        "Host bravo\n    HostName b\n\n"
                        "Host charlie\n    HostName c\n")
        saved = (hosts.SSH_CONFIG, gui.ORDER_STATE, gui.STARS_STATE)
        hosts.SSH_CONFIG = conf
        gui.ORDER_STATE = Path(tmp) / "order"
        gui.STARS_STATE = Path(tmp) / "stars"
        try:
            window.load_hosts(connect=False)    # first run stars all four
            check("the list starts in config order",
                  window.order == ["local", "alpha", "bravo", "charlie"],
                  f"order={window.order}")
            window.move_host("charlie", -1)
            check("moving a server up swaps it with the one above",
                  window.order == ["local", "alpha", "charlie", "bravo"],
                  f"order={window.order}")
            check("and the arrangement is written down",
                  gui.ORDER_STATE.exists()
                  and gui.read_order() == ["local", "alpha", "charlie", "bravo"],
                  f"saved={gui.read_order()}")
            window.load_hosts(connect=False)
            check("which a fresh read of the config preserves",
                  window.order == ["local", "alpha", "charlie", "bravo"],
                  f"order={window.order}")
            window.move_host("local", -1)
            check("and the top server has nowhere further up to go",
                  window.order[0] == "local", f"order={window.order}")
        finally:
            hosts.SSH_CONFIG, gui.ORDER_STATE, gui.STARS_STATE = saved
            window.load_hosts(connect=False)

    # -- mountpoints that outlived their mount -----------------------------
    with _tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "mnt"
        root.mkdir()
        (root / "stale").mkdir()
        keeper = root / "has-files"
        keeper.mkdir()
        (keeper / "notes.txt").write_text("written while unmounted")
        was, hosts.MNT_ROOT = hosts.MNT_ROOT, root
        try:
            gone = hosts.tidy_mounts()
            check("an empty mountpoint is taken away", gone == ["stale"],
                  f"removed={gone}")
            check("and one with anything in it is left alone",
                  keeper.exists() and (keeper / "notes.txt").exists(),
                  "rmdir cannot remove a directory with files in it, which is "
                  "the whole reason this is safe to run at startup")
        finally:
            hosts.MNT_ROOT = was

    # -- forgetting a server, above the config edit --------------------------
    # remove_host is proven against its own file above; this drives the
    # window's half: the views, the watcher, the bookkeeping and the star
    # all have to go with the Host block. The host is a fixture in a config
    # of this run's own, and nothing ever sshes to it -- forget_host edits
    # files and window state, which is the point.
    with _tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        conf.write_text("Host conntest-remote\n    HostName remote.invalid\n")
        was, hosts.SSH_CONFIG = hosts.SSH_CONFIG, conf
        real_close = window.close_session
        closed: list = []

        class View:
            host, name = "conntest-remote", "deploy"

        class Watcher:
            terminated = False

            def terminate(self):
                self.terminated = True

        watcher = Watcher()
        try:
            anchor = next((r for r in rows(window)
                           if getattr(r, "key", None) is not None), None)
            on_named = menu_items(window.host_menu, anchor,
                                  "conntest-remote", 0, 0)
            check("a host that is in the config can be forgotten",
                  any("Forget conntest-remote" in (label or "")
                      for label in on_named),
                  f"menu={on_named}")

            window.close_session = lambda view: (
                closed.append((view.host, view.name)),
                window.open.pop((view.host, view.name), None))
            window.rows["conntest-remote"] = hosts.blank("conntest-remote")
            window.probed["conntest-remote"] = 1.0
            window.streamed["conntest-remote"] = 1.0
            window.inflight.add("conntest-remote")
            window.watchers["conntest-remote"] = watcher
            window.starred.add("conntest-remote")
            window.open[("conntest-remote", "deploy")] = View()

            window.forget_host("conntest-remote")
            check("forgetting a server closes its views and no others",
                  closed == [("conntest-remote", "deploy")]
                  and all(k[0] != "conntest-remote" for k in window.open),
                  f"closed={closed}")
            check("its watcher is terminated and dropped",
                  watcher.terminated
                  and "conntest-remote" not in window.watchers)
            check("and every trace leaves the books",
                  not any("conntest-remote" in place for place in
                          (window.rows, window.probed, window.streamed,
                           window.inflight)))
            check("the star goes too, and is written down",
                  "conntest-remote" not in window.starred
                  and "conntest-remote" not in (gui.read_stars() or set()))
            check("the Host block is out of the config",
                  "conntest-remote" not in conf.read_text())
            check("with the old config kept beside it",
                  any(entry.name.startswith("config.bak.")
                      for entry in Path(tmp).iterdir()))
            check("and the footer says what happened",
                  "forgot conntest-remote" in window.footnote.get_text(),
                  f"footer={window.footnote.get_text()!r}")
        finally:
            window.close_session = real_close
            hosts.SSH_CONFIG = was

    # -- a notification, and the click that answers it -----------------------
    # The builder's fields are checked hermetically in test_hosts.py; here
    # its actual output drives the click handler, because the two only agree
    # by both going through session_ref/split_ref -- change the target format
    # in one place and this is the check that fails.
    Gio = gui.Gio
    grab = {}
    orig_target = Gio.Notification.set_default_action_and_target

    def observed(note, action, target):
        grab["action"], grab["target"] = action, target
        return orig_target(note, action, target)

    Gio.Notification.set_default_action_and_target = observed
    try:
        gui.notification("local", {
            "name": beta[1],
            "agent": {"state": agent_state.NEEDS_YOU, "label": "needs you",
                      "detail": "Do you want to?"}})
    finally:
        Gio.Notification.set_default_action_and_target = orig_target
    check("the notification's click carries the shared ref",
          grab.get("action") == "app.open-session"
          and gui.split_ref(grab["target"].get_string()) == beta,
          f"grabbed={grab}")

    # The cold half of one-window: the click arrives first, do_activate
    # never runs, and the fresh window must be presented with the named
    # session open in it -- the historical bug window()'s docstring
    # describes. A second ConnApp stands in for the cold process; the panel
    # above keeps its own app and window. It has to be registered before it
    # can hold a window (GApplication forbids windows before ::startup), and
    # it takes a distinct, non-unique id so it neither owns nor is answered
    # by the real conn already holding APP_ID on the session bus.
    app2 = gui.ConnApp()
    app2.set_application_id(gui.APP_ID + ".test.cold")
    app2.set_flags(app2.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    app2.register(None)
    app2.open_from_notification(None, grab["target"])
    twos = app2.get_windows()
    check("a click with no window builds exactly one", len(twos) == 1,
          f"windows={len(twos)}")
    window2 = twos[0] if twos else None
    if window2 is not None:
        window2.notify = lambda host, s: None   # nothing reaches the desktop
        check("a fresh panel, not the one above",
              isinstance(window2, gui.Conn) and window2 is not window)
        current = window2.stack.get_visible_child()
        check("with the named session open and showing",
              beta in window2.open and isinstance(current, gui.Session)
              and (current.host, current.name) == beta,
              f"open={list(window2.open)}")
        app2.open_from_notification(None, grab["target"])
        check("and a second click reuses it",
              len(app2.get_windows()) == 1
              and gui.ConnApp.window(app2) is window2)
        for view in list(window2.open.values()):
            window2.close_session(view)
        window2.destroy()

    # -- secrets, in the real keyring ---------------------------------------
    # The one check that touches real user state, so it is opt-in: it proves
    # the secret-tool plumbing against the actual daemon, which no fake can.
    # The entry is cleared on the way out whatever the verdicts were.
    if os.environ.get("CONN_TEST_KEYRING") == "1":
        try:
            hosts.secret_store("conntest-host", "db", "pa55w0rd")
            check("a secret goes into the keyring",
                  hosts.secret_names("conntest-host") == ["db"])
            check("and comes back out by name",
                  hosts.secret_value("conntest-host", "db") == "pa55w0rd")
        finally:
            try:
                hosts.secret_clear("conntest-host", "db")
            except (hosts.HostError, OSError):
                pass
        check("and can be forgotten", hosts.secret_names("conntest-host") == [])
    else:
        print("keyring round trip skipped -- CONN_TEST_KEYRING=1 runs it")

    # -- the Site Manager ---------------------------------------------------
    # The window above came up on the empty config. A config of two named
    # hosts is written now so the manager has something to list, and the three
    # calls that would leave this machine -- resolve() and probe() shelling out
    # to ssh, migrate_secrets() to the real keyring -- are stubbed for the
    # duration. What is under test is the manager's own file edits, which go
    # through the temp config; the keyring move is asserted by the stub having
    # been called, the round trip itself being covered above under
    # CONN_TEST_KEYRING.
    hosts.SSH_CONFIG.write_text(
        "Host example1\n"
        "    HostName 10.0.0.1\n"
        "    User alice\n"
        "    Port 2222\n"
        "\n"
        "Host example2\n"
        "    HostName box.example.com\n")
    # As the real app does after any config change: pick the new hosts up so
    # self.order (which the manager's reorder swaps in) knows about them.
    window.load_hosts(connect=False)

    def pump(predicate, limit=5.0):
        # Save runs update_host on a worker and reports back on an idle, so the
        # check waits on the file rather than guessing how long that takes.
        ctx = gui.GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate() and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)

    def manager_aliases():
        return [getattr(w, "alias", None) for w in walk(window.manager)
                if getattr(w, "alias", None) is not None]

    real_resolve, real_probe = hosts.resolve, hosts.probe
    real_migrate = hosts.migrate_secrets
    migrated: list = []
    hosts.resolve = lambda h: {"user": "", "hostname": h,
                               "port": "22", "identityfile": ""}
    hosts.probe = lambda h: hosts.blank(h)
    hosts.migrate_secrets = lambda old, new: migrated.append((old, new))
    try:
        window.open_manager()
        check("the Site Manager lists the config's hosts",
              manager_aliases() == ["example1", "example2"],
              f"aliases={manager_aliases()}")

        window._manager_reload("example1")
        got = {k: e.get_text() for k, e in window.manager_fields.items()}
        check("selecting a host prefills its fields from host_config",
              got == {"hostname": "10.0.0.1", "user": "alice",
                      "port": "2222", "identityfile": ""},
              f"got={got}")

        window.manager_fields["hostname"].set_text("10.0.0.9")
        window.manager_fields["port"].set_text("22")   # ssh's default: dropped
        window._manager_save()
        pump(lambda: hosts.host_config("example1")["hostname"] == "10.0.0.9")
        saved = hosts.host_config("example1")
        check("Save writes the fields back through update_host",
              saved["hostname"] == "10.0.0.9" and saved["user"] == "alice"
              and saved["port"] == "", f"saved={saved}")

        # Reordering from the manager writes to the same order the sidebar reads.
        window._manager_reload("example2")
        before = window._manager_hosts()
        window.manager_move(-1)
        on_disk = [h for h in gui.read_order() if h in hosts.config_hosts()]
        check("Move up in the manager reorders the servers and persists",
              window._manager_hosts() == list(reversed(before))
              and on_disk == list(reversed(before)),
              f"before={before} after={window._manager_hosts()} disk={on_disk}")
        window.manager_move(-1)
        check("and a server already at the top does not move past it",
              window._manager_hosts()[0] == "example2",
              f"order={window._manager_hosts()}")

        # A rename is the whole migration: config, keyring (stubbed), the star
        # and the place in the hand-arranged order. The window is put in a
        # known state first.
        window.starred = {"example1"}
        gui.save_stars(window.starred)
        window.order = ["example1", "example2"]
        gui.save_order(window.order)
        window.migrate_host("example1", "webhost")

        check("Rename moves the alias in the config",
              "webhost" in hosts.config_hosts()
              and "example1" not in hosts.config_hosts(),
              f"config={hosts.config_hosts()}")
        check("and the block travels with it, unchanged",
              hosts.host_config("webhost")["user"] == "alice",
              f"webhost={hosts.host_config('webhost')}")
        # The keyring migration runs on a worker so a locked keyring cannot
        # freeze the window, so wait for it rather than reading it too soon.
        pump(lambda: migrated)
        check("and migrate_secrets is called to carry the keyring",
              migrated == [("example1", "webhost")], f"migrated={migrated}")
        check("and the star moves to the new name",
              "webhost" in window.starred and "example1" not in window.starred,
              f"starred={window.starred}")
        check("and it keeps its place in the order, in memory and on disk",
              window.order[0] == "webhost" and "example1" not in window.order
              and gui.read_order()[0] == "webhost",
              f"order={window.order} file={gui.read_order()}")
        check("and the manager's own list is redrawn under the new name",
              "webhost" in manager_aliases() and "example1" not in manager_aliases(),
              f"aliases={manager_aliases()}")
    finally:
        hosts.resolve, hosts.probe = real_resolve, real_probe
        hosts.migrate_secrets = real_migrate
        if window.manager is not None:
            window.manager.close()   # emits close-request -> clears self.manager


def rows(window):
    index = 0
    while (row := window.list.get_row_at_index(index)) is not None:
        yield row
        index += 1


def row_for(window, key):
    return next((r for r in rows(window) if getattr(r, "key", None) == key), None)


if __name__ == "__main__":
    raise SystemExit(main())
