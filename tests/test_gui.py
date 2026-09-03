"""What the window does, checked without a pair of hands.

Every bug this file covers was found by hand first, which is the argument for
it: switching that moved the terminal and left the list behind, a menu whose
parent was rebuilt out from under it, controls kept somewhere fullscreen takes
away. None of those are visible to a test of hosts.py, and all of them are one
assertion each here.

Needs a display, because GTK needs one -- it skips rather than fails without.
Real tmux sessions are created on this machine and killed again at the end;
nothing remote is touched.

    python3 tests/test_gui.py
"""

from __future__ import annotations

import os
import subprocess
import sys
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

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Vte", "3.91")
    from gi.repository import GLib, Gtk

    import agent_state
    import gui
    import hosts

    check = Checks()
    for name in SESSIONS:
        tmux("new-session", "-d", "-s", name, "bash --norc")

    # The window builds on activate, but its first probe lands on a worker,
    # so the checks wait for that to come back before asking what is listed.
    class Panel(Gtk.Application):
        def do_activate(self):
            window = gui.Conn(self)

            def later():
                try:
                    run(window, check, gui, hosts, agent_state, Gtk)
                finally:
                    for session in list(window.open.values()):
                        window.close_session(session)
                    self.quit()
                return False

            GLib.timeout_add_seconds(5, later)

    Panel(application_id="org.omarchy.conn.test").run(None)

    for name in (*SESSIONS, NEW_SESSION):
        tmux("kill-session", "-t", name)

    print(f"\n{'all good' if not check.failures else str(check.failures) + ' failed'}")
    return 1 if check.failures else 0


def run(window, check, gui, hosts, agent_state, Gtk) -> None:
    alpha = ("local", SESSIONS[0])
    beta = ("local", SESSIONS[1])

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
    check("one session waiting puts the fleet at red alert",
          window.alert == agent_state.NEEDS_YOU, f"alert={window.alert}")
    session["agent"] = dict(session["agent"], state=agent_state.WORKING,
                            label="working", detail="")
    window.render()
    check("nothing waiting but something working is yellow",
          window.alert == agent_state.WORKING, f"alert={window.alert}")
    session["agent"] = dict(session["agent"], state=agent_state.READY,
                            label="idle", detail="")
    window.render()
    check("and a quiet fleet is all clear",
          window.alert == agent_state.READY, f"alert={window.alert}")

    # -- notifications -----------------------------------------------------
    sent = []
    window.notify = lambda host, s: sent.append((host, s["name"], s["agent"]["state"]))
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
    # so it is announced on the silence, not on the first keystroke.
    sent.clear()
    session["agent"] = dict(session["agent"], state=agent_state.DRAFT,
                            label="unsent draft", detail="what's the health")
    window.render()
    check("typing is not an interruption", not sent, f"sent={sent}")

    def rewind(by=gui.DRAFT_DWELL + 1):
        text, since, told = window.drafts[alpha]
        window.drafts[alpha] = (text, since - by, told)

    session["agent"] = dict(session["agent"], detail="what's the health check")
    rewind()
    window.render()
    check("and an edit puts the clock back", not sent, f"sent={sent}")

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
    icons = [c.get_icon_name() for c in walk(window.get_child().get_start_child())
             if isinstance(c, Gtk.Button) and c.get_icon_name()]
    check("every action lives in the sidebar",
          {"list-add-symbolic", "network-server-symbolic",
           "view-refresh-symbolic", "window-close-symbolic"} <= set(icons),
          f"icons={icons}")
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

        gui.ZOOM_STATE = Path(tmp) / "zoom"
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
    window.stamp -= 10                     # as if a newer copy had landed
    window.check_source()
    check("and one as soon as a newer one is copied over it",
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
    named = [h for h in hosts.config_hosts()]
    if named:
        on_named = menu_items(window.host_menu, row_for(window, alpha),
                              named[0], 0, 0)
        check("but one that is in ~/.ssh/config can be forgotten",
              any(f"Forget {named[0]}" in (label or "") for label in on_named),
              f"menu={on_named}")

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

    # -- secrets -----------------------------------------------------------
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

    hosts.secret_store("conntest-host", "db", "pa55w0rd")
    check("a secret goes into the keyring",
          hosts.secret_names("conntest-host") == ["db"])
    check("and comes back out by name",
          hosts.secret_value("conntest-host", "db") == "pa55w0rd")
    hosts.secret_clear("conntest-host", "db")
    check("and can be forgotten", hosts.secret_names("conntest-host") == [])


def rows(window):
    index = 0
    while (row := window.list.get_row_at_index(index)) is not None:
        yield row
        index += 1


def row_for(window, key):
    return next((r for r in rows(window) if getattr(r, "key", None) == key), None)


if __name__ == "__main__":
    raise SystemExit(main())
