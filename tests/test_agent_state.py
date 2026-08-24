#!/usr/bin/env python3
"""Checks the screen reader against real captures.

Run it with plain python -- there is no test dependency on purpose:

    python3 tests/test_agent_state.py

The opencode fixtures are genuine `tmux capture-pane` output from opencode
1.18.19, one file per state, with only the working directory in the footer
rewritten to /home/you/demo. The Claude Code screens are built here from its
chrome rather than captured, because a real capture carries whatever the
session was actually working on.

The point of this file is the last rule in agent_state's docstring: an
unreadable screen must come back UNKNOWN, never READY. A wrong READY is what
makes you walk away from a session that is sitting blocked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_state as A  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULE = "─" * 100

CLAUDE = {
    "busy": f"✶ Coalescing…\n\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · esc to interrupt",
    "permission": f"Do you want to make this edit?\n 1. Yes\n{RULE}\n❯ \n{RULE}",
    "draft": f"{RULE}\n❯ finish the migration\n{RULE}\n  ⏵⏵ auto mode on",
    "idle": f"✻ Cooked for 12s\n{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n  ⏵⏵ auto mode on",
    "background": f"{RULE}\n❯ \n{RULE}\n  3/8 agents done · 1m 2s ·",
}

CASES = [
    # (name, screen, commands, expected state)
    ("opencode idle",        "opencode_idle.txt",             ["opencode"], A.READY),
    ("opencode draft",       "opencode_draft.txt",            ["opencode"], A.DRAFT),
    ("opencode working",     "opencode_working.txt",          ["opencode"], A.WORKING),
    ("opencode permission",  "opencode_needs_you.txt",        ["opencode"], A.NEEDS_YOU),
    ("opencode after turn",  "opencode_ready_after_turn.txt", ["opencode"], A.READY),

    ("claude busy",          CLAUDE["busy"],       ["claude"], A.WORKING),
    ("claude permission",    CLAUDE["permission"], ["claude"], A.NEEDS_YOU),
    ("claude draft",         CLAUDE["draft"],      ["claude"], A.DRAFT),
    ("claude idle",          CLAUDE["idle"],       ["claude"], A.READY),
    ("claude background",    CLAUDE["background"], ["claude"], A.WORKING),

    # Not an agent at all.
    ("plain shell",          "opencode_idle.txt",  ["bash"],   A.SHELL),
    # Unreadable input must never come back as READY.
    ("empty screen",         "",                   ["claude"], A.UNKNOWN),
    ("no chrome",            "just some text",     ["claude"], A.UNKNOWN),
    ("truncated opencode",   "┃  half a box",      ["opencode"], A.UNKNOWN),
]


def screen_for(value: str) -> str:
    """A case carries either a fixture filename or a screen written inline."""
    if value.endswith(".txt"):
        return (FIXTURES / value).read_text()
    return value


def main() -> int:
    failures = 0
    for name, value, commands, expected in CASES:
        result = A.classify(screen_for(value), commands)
        ok = result["state"] == expected
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:22} {result['state']:9} "
              f"{result['detail'][:40]!r}")
        if not ok:
            print(f"     expected {expected}")

    # The details that make the panel useful, not just correct.
    checks = [
        ("opencode reports context",
         A.classify(screen_for("opencode_ready_after_turn.txt"),
                    ["opencode"])["tokens"] == "7.4K"),
        ("opencode names the tool it is running",
         A.classify(screen_for("opencode_working.txt"),
                    ["opencode"])["detail"] != "working"),
        ("opencode draft is the typed text",
         A.classify(screen_for("opencode_draft.txt"),
                    ["opencode"])["detail"] == "this draft is never sent"),
        ("claude still reports context",
         A.classify("300.2k tokens\n" + CLAUDE["idle"],
                    ["claude"])["tokens"] == "300.2k"),
        ("the agent is named",
         A.classify(screen_for("opencode_idle.txt"),
                    ["opencode"])["agent"] == "opencode"),
        ("draft and blocked both call for a human",
         A.needs_attention(A.DRAFT) and A.needs_attention(A.NEEDS_YOU)),
        ("working and idle do not",
         not A.needs_attention(A.WORKING) and not A.needs_attention(A.READY)),
    ]
    for label, passed in checks:
        failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {label}")

    print(f"\n{len(CASES) + len(checks) - failures}/{len(CASES) + len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
