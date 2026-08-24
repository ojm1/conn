"""Entry point for the server panel."""

from __future__ import annotations

import sys

USAGE = """helm -- your servers, and what is running on them

  helm                   open the panel
  helm --list            print host names (for scripts)
  helm --check           probe every host once and print the result
  helm --help            this message

Inside the panel:
  ?        the full key guide, without leaving the panel
  enter    reply to the selected chat, without leaving the panel
  enter    (with an empty box) submits whatever draft is already sitting there
  o        open the chat in this window   F12  come back (or ctrl-b d)
  w        open it in its own window     W    open all that need you
  1-9      select a chat                  s  sidebar    v  chats <-> hosts
  n        new shell session              r  refresh
  f / u    mount / unmount                a  add server  k  install your key
  e        edit ~/.ssh/config             /  filter      q  quit

Chat states:  working | needs you | unsent draft | idle | shell

The main pane is a live view of the chat's screen, updated about once a second.
HELM_NO_WATCH=1 disables streaming and falls back to 45s polling.
"""


def main(argv: list[str]) -> int:
    import hosts

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

    from tui import SSHPanel
    SSHPanel().run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
