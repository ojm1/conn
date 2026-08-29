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

os.environ.setdefault("HELM_NO_WATCH", "1")

SESSIONS = ("helmtest-alpha", "helmtest-beta")


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
            window = gui.Helm(self)

            def later():
                try:
                    run(window, check, gui, hosts, agent_state, Gtk)
                finally:
                    for session in list(window.open.values()):
                        window.close_session(session)
                    self.quit()
                return False

            GLib.timeout_add_seconds(5, later)

    Panel(application_id="org.omarchy.helm.test").run(None)

    for name in SESSIONS:
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

    # -- killing -----------------------------------------------------------
    kills = [c for c in walk(row_for(window, alpha))
             if isinstance(c, Gtk.Button) and c.has_css_class("kill")]
    check("each session carries its own kill", len(kills) == 1)

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
    hosts.secret_store("helmtest-host", "db", "pa55w0rd")
    check("a secret goes into the keyring",
          hosts.secret_names("helmtest-host") == ["db"])
    check("and comes back out by name",
          hosts.secret_value("helmtest-host", "db") == "pa55w0rd")
    hosts.secret_clear("helmtest-host", "db")
    check("and can be forgotten", hosts.secret_names("helmtest-host") == [])


def rows(window):
    index = 0
    while (row := window.list.get_row_at_index(index)) is not None:
        yield row
        index += 1


def row_for(window, key):
    return next((r for r in rows(window) if getattr(r, "key", None) == key), None)


if __name__ == "__main__":
    raise SystemExit(main())
