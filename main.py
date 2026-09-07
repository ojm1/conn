"""Entry point for the server panel."""

from __future__ import annotations

import os
import re
import sys

USAGE = """conn -- your servers, and what is running on them

  conn                   open the window
  conn --list            print host names (for scripts)
  conn --check           probe every host once and print the result
  conn --notify          send one notification, to see what one looks like
  conn --help            this message

Inside the window:
  F1 or ?        the key guide -- every key and mouse action
  alt-1..alt-9   open the session with that number
  ctrl-tab       next open session (ctrl-shift-tab for the last)
  F12            back to the list      F11  fullscreen
  ctrl-shift-w   close the view        ctrl-shift-k  kill the session
  ctrl-shift-r   rename the session
  ctrl-shift-c/v copy / paste          ctrl-f        filter
  ctrl-+ - 0     text bigger, smaller, back to the terminal's own size
  ctrl-q         quit
  right-click    a host for new session, files, keys; a session to open or kill

Chat states:  working | needs you | unsent draft | idle | shell | unknown

The list is a live view of every session's screen, updated about once a second.
CONN_NO_WATCH=1 disables streaming and falls back to 45s polling.
CONN_NO_LOCAL=1 drops the 'local' row for this machine.

Sessions use the font this machine's terminal is configured with, so they
match the terminals beside them. CONN_FONT="JetBrainsMono Nerd Font 11"
overrides it, and ctrl-+ / ctrl-- / ctrl-0 zoom, remembered between runs.
CONN_THEME=light|dark forces the colours.
"""

# --check prints strings a remote can influence: ssh's auth banner lands in
# row['error'], and a session is named whatever the far side called it. This
# goes to a terminal, which obeys control characters -- an OSC can retitle
# the window, or write the clipboard where OSC 52 is on -- so none survive.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def main(argv: list[str]) -> int:
    import hosts

    # Before anything spawns an ssh: a conn started from a launcher inherits
    # no SSH_AUTH_SOCK, and would look like it had no agent at all.
    hosts.ensure_agent()

    if len(argv) > 1:
        arg = argv[1]

        if arg in ("-h", "--help"):
            print(USAGE)
            return 0

        if arg == "--list":
            for host in hosts.list_hosts():
                print(host)
            return 0

        if arg == "--check":
            names = hosts.list_hosts()
            if not names:
                print("No hosts in ~/.ssh/config.")
                return 1
            for host in names:
                row = hosts.probe(host)
                sessions = ", ".join(s["name"] for s in row["sessions"]) or "-"
                print(CONTROL.sub("", f"{host:<20} {row['state']:<8} "
                                      f"{sessions:<24} {row['target']}"
                                  + (f"  ({row['error']})"
                                     if row["error"] else "")))
            return 0

        if arg == "--notify":
            try:
                from gui import send_test_notification
            except (ImportError, ValueError) as exc:
                print(f"needs GTK4: {exc}", file=sys.stderr)
                return 1
            return send_test_notification()

        if arg == "--gapplication-service":
            # Not a user flag: the bus daemon passes it when it starts conn
            # to answer an activation (org.omarchy.conn.service). It has to
            # get past this parser to GApplication, which owns it -- register
            # on the bus, run the action that caused the start, and only open
            # a window if that action asks for one.
            try:
                from gui import run
            except (ImportError, ValueError) as exc:
                print(f"needs GTK4: {exc}", file=sys.stderr)
                return 1
            return run(service=True)

        print(f"unknown option: {arg}\n\n{USAGE}", file=sys.stderr)
        return 2

    try:
        from gui import run
    except (ImportError, ValueError) as exc:
        print(f"the window needs GTK4 and VTE: {exc}\n"
              "  sudo pacman -S gtk4 vte4 python-gobject",
              file=sys.stderr)
        return 1
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
