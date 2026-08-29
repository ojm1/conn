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
    # Two finished turns still on screen. The one underneath is the one that
    # just happened; reading forwards reported the older one for as long as it
    # stayed visible.
    "two turns": (f"✻ Cogitated for 26s\n\n  all 6 rendered successfully.\n\n"
                  f"✻ Cooked for 1m 5s\n{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n"
                  f"  ⏵⏵ auto mode on"),
    # Same for a prompt you have already answered, sitting above a live one.
    "two prompts": (f"Do you want to make this edit?\n 1. Yes\n"
                    f"  ⎿ Updated main.py\n\n"
                    f"Do you want to run rm -rf build?\n 1. Yes\n{RULE}\n❯ \n{RULE}"),
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
    ("claude after two turns", CLAUDE["two turns"], ["claude"], A.READY),
    ("claude second prompt",  CLAUDE["two prompts"], ["claude"], A.NEEDS_YOU),

    # Not an agent at all.
    ("plain shell",          "opencode_idle.txt",  ["bash"],   A.SHELL),
    # Unreadable input must never come back as READY.
    ("empty screen",         "",                   ["claude"], A.UNKNOWN),
    ("no chrome",            "just some text",     ["claude"], A.UNKNOWN),
    ("truncated opencode",   "┃  half a box",      ["opencode"], A.UNKNOWN),
]


# Captured off real screens. The words are interchangeable; only the dim SGR
# says which one you are looking at, so these carry their escapes and go in
# raw as well as stripped.
RAW_CASES = [
    ("claude suggestion is not a draft", "claude_suggestion.txt", A.READY),
    ("claude typed text is a draft",     "claude_typed_draft.txt", A.DRAFT),
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

    for name, fixture, expected in RAW_CASES:
        raw = (FIXTURES / fixture).read_text()
        result = A.classify(A.strip_ansi(raw), ["claude"], raw)
        ok = result["state"] == expected
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:34} {result['state']:9} "
              f"{result['detail'][:34]!r}")
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
        ("and the count it reports is the current one",
         A.classify("120.0k tokens\nlater\n559.6k tokens\n" + CLAUDE["idle"],
                    ["claude"])["tokens"] == "559.6k"),
        ("the agent is named",
         A.classify(screen_for("opencode_idle.txt"),
                    ["opencode"])["agent"] == "opencode"),
        ("the timing is the turn that just finished",
         A.classify(CLAUDE["two turns"], ["claude"])["detail"]
         == "Cooked for 1m 5s"),
        ("and the prompt is the one still waiting",
         "rm -rf build" in A.classify(CLAUDE["two prompts"], ["claude"])["detail"]),
        ("draft and blocked both call for a human",
         A.needs_attention(A.DRAFT) and A.needs_attention(A.NEEDS_YOU)),
        ("working and idle do not",
         not A.needs_attention(A.WORKING) and not A.needs_attention(A.READY)),
    ]
    for label, passed in checks:
        failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {label}")

    total = len(CASES) + len(RAW_CASES) + len(checks)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
