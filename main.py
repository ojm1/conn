"""Entry point for the server panel."""

from __future__ import annotations

import os
import sys

USAGE = """helm -- your servers, and what is running on them

  helm                   open the window
  helm --list            print host names (for scripts)
  helm --check           probe every host once and print the result
  helm --help            this message

Inside the window:
  ?              the key guide, at the foot of the list
  alt-1..alt-9   open the session with that number
  ctrl-tab       next open session (ctrl-shift-tab for the last)
  F12            back to the list      F11  fullscreen
  ctrl-shift-w   close the view        ctrl-shift-k  kill the session
  ctrl-shift-c/v copy / paste          ctrl-f        filter
  ctrl-+ - 0     text bigger, smaller, back to the terminal's own size
  ctrl-q         quit
  right-click    a host for new session, files, keys; a session to open or kill

Chat states:  working | needs you | unsent draft | idle | shell

The list is a live view of every session's screen, updated about once a second.
HELM_NO_WATCH=1 disables streaming and falls back to 45s polling.
HELM_NO_LOCAL=1 drops the 'local' row for this machine.

Sessions use the font this machine's terminal is configured with, so they
match the terminals beside them. HELM_FONT="JetBrainsMono Nerd Font 11"
overrides it, and ctrl-+ / ctrl-- / ctrl-0 zoom, remembered between runs.
HELM_THEME=light|dark forces the colours.
"""


def main(argv: list[str]) -> int:
    import hosts

    # Before anything spawns an ssh: a helm started from a launcher inherits
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
                print(f"{host:<20} {row['state']:<8} {sessions:<24} "
                      f"{row['target']}"
                      + (f"  ({row['error']})" if row["error"] else ""))
            return 0

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
