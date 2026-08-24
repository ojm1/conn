"""Reading a Claude Code screen and deciding whether it needs you.

The panel exists to answer one question from a distance: is that chat working,
or is it waiting on me? Claude Code does not publish that anywhere, but it says
so plainly on screen -- so this reads the screen.

Two rules shape everything below:

  * Patterns are loose. Claude Code's wording shifts between releases, and this
    runs against whatever version happens to be on the far box.
  * Anything unrecognised is UNKNOWN, never READY. Telling you a chat is idle
    when it is actually blocked on a permission prompt is the one failure that
    would make the panel worse than not looking.
"""

from __future__ import annotations

import re

WORKING = "working"      # actively thinking or running tools
NEEDS_YOU = "needs-you"  # blocked on a prompt only you can answer
DRAFT = "draft"          # text sitting in the box, never submitted
READY = "ready"          # idle at an empty prompt
SHELL = "shell"          # not a Claude Code session at all
UNKNOWN = "unknown"

# Claude Code prints this next to the spinner the entire time it is busy. It is
# the single most reliable "still going" signal on the screen.
BUSY = re.compile(r"esc to interrupt", re.I)

# Blocked-on-you prompts. Several shapes across versions, so match generously.
BLOCKED = [
    re.compile(r"\bDo you want to\b", re.I),
    re.compile(r"\bWould you like to\b", re.I),
    re.compile(r"^\s*❯?\s*1\.\s*Yes\b", re.M),
    re.compile(r"\(y/n\)", re.I),
    re.compile(r"\bPress enter to continue\b", re.I),
    re.compile(r"\bwaiting for your input\b", re.I),
]

# The input box shows this greyed-out hint only while it is empty.
PLACEHOLDER = re.compile(r'Try\s+"')

# The footer strip that sits under the input box.
FOOTER = re.compile(r"⏵⏵|auto mode on|shift\+tab to cycle", re.I)

# A horizontal rule -- the input box is fenced by two of them. The top rule
# can carry a short label baked into it ("──── ultracode ─"), so a rule is
# "mostly dashes", not "only dashes".
RULE_LABEL_MAX = 24


def _is_rule(line: str) -> bool:
    stripped = line.strip()
    dashes = sum(1 for char in stripped if char in "─━-")
    if dashes < 20:
        return False
    label = [char for char in stripped if char not in "─━- "]
    return len(label) <= RULE_LABEL_MAX

# The live spinner, e.g. "✶ Coalescing…". The trailing ellipsis is what
# separates it from the finished form ("✻ Cooked for 1s").
SPINNER = re.compile(r"^\s*[✶✻✽✢✳✱⋆*]\s*(\S[^\n]*?[….]{1,3})\s*$", re.M)

# "✻ Cogitated for 21m 47s" / "✻ Worked for 20m 36s". Past tense and a real
# duration, so it cannot swallow a live line like "Waiting for 1 workflow".
LAST_ACTION = re.compile(
    r"[✻✽✢⋆*]\s*([A-Z][a-z]+ed\s+for\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s)")

# Work that is running *outside* the main turn: background workflows and
# agent fleets. The main loop sits at an empty prompt while these run, so
# without this the panel would call a busy session idle -- the single most
# misleading thing it could say.
AGENTS = re.compile(r"(\d+)\s*/\s*(\d+)\s+agents?\s+done", re.I)
WORKFLOW = re.compile(r"Waiting for\s+(\d+)\s+(?:dynamic\s+)?workflow", re.I)
ELAPSED = re.compile(r"·\s*((?:\d+h\s*)?(?:\d+m\s*)?\d+s)\s*·")

# "275.1k tokens"
TOKENS = re.compile(r"([0-9.]+k)\s+tokens", re.I)

SHELLS = {"bash", "zsh", "sh", "fish", "-bash", "tmux"}

LABELS = {
    WORKING: "working",
    NEEDS_YOU: "needs you",
    DRAFT: "unsent draft",
    READY: "idle",
    SHELL: "shell",
    UNKNOWN: "unknown",
}


def classify(screen: str, commands: list[str]) -> dict:
    """Return {state, label, detail, tokens} for one session's visible screen.

    `commands` is what the panes are running, which settles the shell case
    without having to infer it from pixels.
    """
    result = {"state": UNKNOWN, "label": LABELS[UNKNOWN], "detail": "",
              "tokens": ""}

    if commands and not any("claude" in c for c in commands):
        result["state"] = SHELL
        result["label"] = LABELS[SHELL]
        result["detail"] = ", ".join(dict.fromkeys(commands))
        return result

    if not screen.strip():
        return result

    match = TOKENS.search(screen)
    if match:
        result["tokens"] = match.group(1)

    # Order matters. Busy beats everything: a permission prompt from a previous
    # turn can still be on screen above a spinner that has moved on.
    if BUSY.search(screen):
        result["state"] = WORKING
        result["label"] = LABELS[WORKING]
        result["detail"] = _busy_detail(screen)
        return result

    for pattern in BLOCKED:
        if pattern.search(screen):
            result["state"] = NEEDS_YOU
            result["label"] = LABELS[NEEDS_YOU]
            result["detail"] = _blocked_detail(screen)
            return result

    box = _input_box(screen)

    # An unsent draft still wins: that one needs a human, background work does
    # not. But anything running in the background beats "idle".
    if box is not None and not box:
        background = _background(screen)
        if background:
            result["state"] = WORKING
            result["label"] = LABELS[WORKING]
            result["detail"] = background
            return result
    if box is None:
        return result

    if box:
        # A draft still means "a human has to do something", but if work is
        # also running in the background, say so -- otherwise you walk into a
        # session expecting it to be idle and find it mid-flight.
        result["state"] = DRAFT
        result["label"] = LABELS[DRAFT]
        background = _background(screen)
        result["detail"] = f"{box}  ({background})" if background else box
        return result

    result["state"] = READY
    result["label"] = LABELS[READY]
    action = LAST_ACTION.search(screen)
    result["detail"] = action.group(1).strip() if action else ""
    return result


def _input_box(screen: str) -> str | None:
    """The contents of the input box, and only that.

    Returns None when the box cannot be located (so the caller says UNKNOWN
    rather than guessing), "" when the box is empty, otherwise the text sitting
    in it unsent.

    This has to be exact. Submitted messages stay on screen in the transcript
    behind the very same "❯" that marks the input line, so matching "❯"
    anywhere reports every finished chat as having unsent text. What actually
    identifies the box is its fencing: it is the region between the last two
    plain horizontal rules. Box-drawing corners like "╰───" are deliberately
    not rules, which is what keeps the welcome banner from matching.
    """
    lines = screen.splitlines()
    rules = [index for index, line in enumerate(lines) if _is_rule(line)]
    if not rules:
        return None

    if len(rules) >= 2 and rules[-1] - rules[-2] <= 4:
        segment = lines[rules[-2] + 1:rules[-1]]
    else:
        # The capture can end mid-box when the screen was trimmed, leaving the
        # opening rule as the last one seen.
        segment = lines[rules[-1] + 1:]

    segment = [line for line in segment if not FOOTER.search(line)]
    if not segment:
        return ""

    text = " ".join(part.strip() for part in segment).strip()
    text = re.sub(r"^(?:❯|>)\s*", "", text).strip()

    if not text or PLACEHOLDER.search(text):
        return ""
    return text[:60]


def _background(screen: str) -> str:
    """Describe background work, or "" if there is none running."""
    agents = AGENTS.search(screen)
    if agents:
        done, total = int(agents.group(1)), int(agents.group(2))
        if done < total:
            line = screen[agents.start():].splitlines()[0]
            elapsed = ELAPSED.search(line)
            suffix = f" - {elapsed.group(1)}" if elapsed else ""
            return f"{done}/{total} agents{suffix}"

    workflow = WORKFLOW.search(screen)
    if workflow:
        count = workflow.group(1)
        return f"{count} workflow{'s' if count != '1' else ''} running"
    return ""


def _busy_detail(screen: str) -> str:
    """What it is busy doing, e.g. 'Coalescing...'.

    The "esc to interrupt" marker lives in the footer strip, which says nothing
    useful, so the detail comes from the spinner line instead.
    """
    spins = SPINNER.findall(screen)
    if spins:
        return spins[-1].strip()[:60]
    action = LAST_ACTION.search(screen)
    return action.group(1).strip()[:60] if action else "working"


def _blocked_detail(screen: str) -> str:
    for pattern in BLOCKED:
        match = pattern.search(screen)
        if match:
            line = screen[match.start():].splitlines()[0]
            return line.strip().strip("│ ")[:60]
    return "waiting for you"


def needs_attention(state: str) -> bool:
    """The states where a human has to do something before anything moves."""
    return state in (NEEDS_YOU, DRAFT)
