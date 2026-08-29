"""Reading a coding agent's screen and deciding whether it needs you.

The panel exists to answer one question from a distance: is that chat working,
or is it waiting on me? Neither agent publishes that anywhere, but both say so
plainly on screen -- so this reads the screen.

Three rules shape everything below:

  * Patterns are loose. The wording shifts between releases, and this runs
    against whatever version happens to be on the far box.
  * Anything unrecognised is UNKNOWN, never READY. Telling you a chat is idle
    when it is actually blocked on a permission prompt is the one failure that
    would make the panel worse than not looking.
  * The states are the agent's, but the *ordering* of the checks is not: busy
    beats blocked beats draft beats idle, whichever agent is on screen. Each
    agent supplies patterns; `classify` owns the decision.

Supported agents are the classes in AGENTS. Adding a third means writing its
patterns, not touching the caller.
"""

from __future__ import annotations

import re

WORKING = "working"      # actively thinking or running tools
NEEDS_YOU = "needs-you"  # blocked on a prompt only you can answer
DRAFT = "draft"          # text sitting in the box, never submitted
READY = "ready"          # idle at an empty prompt
SHELL = "shell"          # not an agent session at all
UNKNOWN = "unknown"

# What a dialog is allowed to draw in front of its own text: box bars, carets,
# bullets, indentation. Quote marks are deliberately not in it -- see BLOCKED.
CHROME = r"""^[^\w\n"“”'`]*"""

LABELS = {
    WORKING: "working",
    NEEDS_YOU: "needs you",
    DRAFT: "unsent draft",
    READY: "idle",
    SHELL: "shell",
    UNKNOWN: "unknown",
}


class Agent:
    """What one agent's screen looks like.

    Subclasses supply patterns and, where the layout demands it, their own
    input-box parser. Everything else is shared.
    """

    key = ""
    name = ""
    COMMANDS: tuple[str, ...] = ()

    BUSY: re.Pattern | None = None
    BLOCKED: tuple[re.Pattern, ...] = ()
    TOKENS: re.Pattern | None = None

    def input_box(self, screen: str,
                  ghosts: set[int] | None = None) -> str | None:
        """Contents of the input box: None if it cannot be found, "" if empty,
        otherwise the unsent text."""
        raise NotImplementedError

    def busy_detail(self, screen: str) -> str:
        return "working"

    def idle_detail(self, screen: str) -> str:
        return ""

    def background(self, screen: str) -> str:
        """Work running outside the main turn, or "" when there is none."""
        return ""

    def tokens(self, screen: str) -> str:
        """The last count on screen: a transcript can carry older ones from a
        /context or a compaction notice, and the footer is below all of them."""
        return last_match(self.TOKENS, screen) if self.TOKENS else ""

    def blocked_detail(self, screen: str) -> str:
        """The newest match of the most telling pattern.

        Pattern order is a preference -- the question a prompt asks says more
        than the "1. Yes" underneath it -- so that is kept. Within one pattern
        it is the *last* match that matters: a prompt you already answered
        stays in the transcript above the one still waiting, and quoting the
        old one is how the panel described the wrong prompt.
        """
        for pattern in self.BLOCKED:
            newest = None
            for match in pattern.finditer(screen):
                newest = match
            if newest is not None:
                line = screen[newest.start():].splitlines()[0]
                return line.strip().strip("│┃ ")[:60]
        return "waiting for you"


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

class ClaudeCode(Agent):
    key = "claude"
    name = "Claude Code"
    COMMANDS = ("claude",)

    # Printed next to the spinner the entire time it is busy. The single most
    # reliable "still going" signal on the screen.
    BUSY = re.compile(r"esc to interrupt", re.I)

    # Blocked-on-you prompts. Several shapes across versions, so match
    # generously.
    # Anchored to the start of a line, past whatever chrome is drawn in front
    # of it -- box bars, carets, bullets, all of which are non-word characters.
    # A prompt Claude Code is actually asking begins its line. The same words
    # inside a sentence are someone *talking* about a prompt, and reading those
    # as one is how a session discussing this file reported itself blocked.
    #
    # Quote marks are excluded from that chrome even though they are not word
    # characters: a dialog never draws one in front of its question, and prose
    # quoting a prompt lands one there every time -- including halfway down a
    # sentence, where the terminal wrapped the line and left the quote at the
    # front of the next one.
    BLOCKED = (
        re.compile(CHROME + r"Do you want to\b", re.I | re.M),
        re.compile(CHROME + r"Would you like to\b", re.I | re.M),
        re.compile(r"^\s*❯?\s*1\.\s*Yes\b", re.M),
        re.compile(r"\(y/n\)\s*$", re.I | re.M),
        re.compile(CHROME + r"Press enter to continue\b", re.I | re.M),
        re.compile(CHROME + r"waiting for your input\b", re.I | re.M),
    )

    # "275.1k tokens"
    TOKENS = re.compile(r"([0-9.]+k)\s+tokens", re.I)

    # The input box shows this greyed-out hint only while it is empty.
    PLACEHOLDER = re.compile(r'Try\s+"')

    # The footer strip that sits under the input box.
    FOOTER = re.compile(r"⏵⏵|auto mode on|shift\+tab to cycle", re.I)

    # The live spinner, e.g. "✶ Coalescing…". The trailing ellipsis is what
    # separates it from the finished form ("✻ Cooked for 1s").
    SPINNER = re.compile(r"^\s*[✶✻✽✢✳✱⋆*]\s*(\S[^\n]*?[….]{1,3})\s*$", re.M)

    # "✻ Cogitated for 21m 47s". Past tense and a real duration, so it cannot
    # swallow a live line like "Waiting for 1 workflow".
    LAST_ACTION = re.compile(
        r"[✻✽✢⋆*]\s*([A-Z][a-z]+ed\s+for\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s)")

    # Work running *outside* the main turn: background workflows and agent
    # fleets. The main loop sits at an empty prompt while these run, so without
    # this the panel would call a busy session idle.
    AGENTS = re.compile(r"(\d+)\s*/\s*(\d+)\s+agents?\s+done", re.I)
    WORKFLOW = re.compile(r"Waiting for\s+(\d+)\s+(?:dynamic\s+)?workflow", re.I)
    ELAPSED = re.compile(r"·\s*((?:\d+h\s*)?(?:\d+m\s*)?\d+s)\s*·")

    # A horizontal rule -- the input box is fenced by two of them. The top rule
    # can carry a short label baked into it ("──── ultracode ─"), so a rule is
    # "mostly dashes", not "only dashes".
    RULE_LABEL_MAX = 24

    def _is_rule(self, line: str) -> bool:
        stripped = line.strip()
        dashes = sum(1 for char in stripped if char in "─━-")
        if dashes < 20:
            return False
        label = [char for char in stripped if char not in "─━- "]
        return len(label) <= self.RULE_LABEL_MAX

    def input_box(self, screen: str,
                  ghosts: set[int] | None = None) -> str | None:
        """This has to be exact. Submitted messages stay on screen in the
        transcript behind the very same "❯" that marks the input line, so
        matching "❯" anywhere reports every finished chat as having unsent
        text. What actually identifies the box is its fencing: it is the region
        between the last two plain horizontal rules. Box-drawing corners like
        "╰───" are deliberately not rules, which is what keeps the welcome
        banner from matching.
        """
        lines = screen.splitlines()
        rules = [index for index, line in enumerate(lines) if self._is_rule(line)]
        if not rules:
            return None

        if len(rules) >= 2 and rules[-1] - rules[-2] <= 4:
            segment = lines[rules[-2] + 1:rules[-1]]
        else:
            # The capture can end mid-box when the screen was trimmed, leaving
            # the opening rule as the last one seen.
            segment = lines[rules[-1] + 1:]

        # Whatever the box holds, if it was drawn dim then Claude suggested
        # it and you did not leave it there. An empty box is idle.
        if ghosts:
            first = (rules[-2] + 1
                     if len(rules) >= 2 and rules[-1] - rules[-2] <= 4
                     else rules[-1] + 1)
            segment = [line for offset, line in enumerate(segment)
                       if first + offset not in ghosts]

        segment = [line for line in segment if not self.FOOTER.search(line)]
        if not segment:
            return ""

        text = " ".join(part.strip() for part in segment).strip()
        text = re.sub(r"^(?:❯|>)\s*", "", text).strip()

        if not text or self.PLACEHOLDER.search(text):
            return ""
        return text[:60]

    def busy_detail(self, screen: str) -> str:
        """The "esc to interrupt" marker lives in the footer strip, which says
        nothing useful, so the detail comes from the spinner line instead."""
        spins = self.SPINNER.findall(screen)
        if spins:
            return spins[-1].strip()[:60]
        return last_match(self.LAST_ACTION, screen)[:60] or "working"

    def idle_detail(self, screen: str) -> str:
        return last_match(self.LAST_ACTION, screen)

    def background(self, screen: str) -> str:
        agents = self.AGENTS.search(screen)
        if agents:
            done, total = int(agents.group(1)), int(agents.group(2))
            if done < total:
                line = screen[agents.start():].splitlines()[0]
                elapsed = self.ELAPSED.search(line)
                suffix = f" - {elapsed.group(1)}" if elapsed else ""
                return f"{done}/{total} agents{suffix}"

        workflow = self.WORKFLOW.search(screen)
        if workflow:
            count = workflow.group(1)
            return f"{count} workflow{'s' if count != '1' else ''} running"
        return ""


# --------------------------------------------------------------------------
# opencode
# --------------------------------------------------------------------------

class OpenCode(Agent):
    """opencode draws nothing like Claude Code.

    Its input box is fenced down the *left* by a heavy vertical bar and closed
    underneath by a single rule of upper-half blocks -- no top rule at all. The
    transcript uses the same left bar for the messages you have already sent,
    so the bar alone identifies nothing; the bottom rule is what anchors the
    box. Captured from opencode 1.18.19.
    """

    key = "opencode"
    name = "opencode"
    COMMANDS = ("opencode",)

    # The footer hint shown only while a turn is running. It becomes "esc again
    # to interrupt" once esc has been pressed a first time.
    BUSY = re.compile(r"\besc\s+(?:again\s+to\s+)?interrupt\b", re.I)

    # Same rule as Claude Code's: the dialog's own lines, not a sentence that
    # happens to contain the words.
    BLOCKED = (
        re.compile(CHROME + r"Permission required", re.I | re.M),
        re.compile(CHROME + r"Allow once\b", re.I | re.M),
        re.compile(CHROME + r"Allow always\b.*\bReject\b", re.I | re.M),
    )

    # The idle footer carries the context used, e.g. "7.4K (3%) · $0.00".
    TOKENS = re.compile(r"([0-9.]+[KM])\s*\(\s*\d+\s*%\s*\)")

    BAR = "┃"                      # U+2503, fences the box and the transcript
    RULE = re.compile(r"^\s*╹▀+\s*$")   # U+2579 corner, then upper-half blocks

    PLACEHOLDER = re.compile(r"Ask anything", re.I)

    # The last line inside the box is opencode's own status ("Build · Kimi K2.7
    # Code OpenCode Go"), not something you typed.
    STATUS = re.compile(r"·")

    # A tool running inside the box, e.g. "⠋ rm -f demo.py".
    SPINNER = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*(\S[^\n]*)")

    # A finished turn stamps the elapsed time onto the model line:
    # "▣  Build · Kimi K2.7 Code · 4.7s".
    LAST_ACTION = re.compile(
        r"▣[^\n]*·\s*((?:\d+h\s*)?(?:\d+m\s*)?[\d.]+s)\s*$", re.M)

    def input_box(self, screen: str,
                  ghosts: set[int] | None = None) -> str | None:
        lines = screen.splitlines()
        rules = [index for index, line in enumerate(lines)
                 if self.RULE.match(line)]
        if not rules:
            # No bottom rule: either not opencode, or a dialog has taken the
            # box over. Either way this must not guess.
            return None

        end = rules[-1]
        start = end
        while start > 0 and self.BAR in lines[start - 1]:
            start -= 1
        segment = lines[start:end]
        if not segment:
            return ""

        # Drop opencode's own status line, which always sits at the bottom of
        # the box and would otherwise read as unsent text on every session.
        if segment and self.STATUS.search(segment[-1]):
            segment = segment[:-1]

        parts = []
        for line in segment:
            _, _, rest = line.partition(self.BAR)
            parts.append(rest.strip())
        text = " ".join(part for part in parts if part).strip()

        if not text or self.PLACEHOLDER.search(text):
            return ""
        return text[:60]

    def busy_detail(self, screen: str) -> str:
        spins = self.SPINNER.findall(screen)
        if spins:
            return spins[-1].strip().strip("│┃ ")[:60]
        return "working"

    def idle_detail(self, screen: str) -> str:
        return last_match(self.LAST_ACTION, screen)


AGENTS: tuple[Agent, ...] = (ClaudeCode(), OpenCode())

SHELLS = {"bash", "zsh", "sh", "fish", "-bash", "tmux"}


def detect(commands: list[str]) -> Agent | None:
    """Which agent is running in these panes, if any.

    `commands` is what the panes are running, which settles the shell case
    without having to infer it from pixels.
    """
    for agent in AGENTS:
        for command in commands:
            if any(name in command for name in agent.COMMANDS):
                return agent
    return None


SGR = re.compile(r"\x1b\[([0-9;]*)m")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def last_match(pattern: re.Pattern, screen: str) -> str:
    """The last time a pattern appears on the screen, which is the most recent
    one. A terminal scrolls: the *first* match is the oldest turn still
    visible, so searching forwards reported a session's timing from something
    it finished several turns ago -- "Cogitated for 26s" while the screen
    plainly said "Cooked for 1m 5s" underneath it.
    """
    found = ""
    for match in pattern.finditer(screen):
        found = match.group(1).strip()
    return found


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def ghost_lines(raw: str) -> set[int]:
    """Line numbers whose text is mostly dim.

    Claude Code writes its suggested next prompt into the input box in dim
    grey -- SGR 2 -- exactly where a half-typed message would sit. Stripped of
    colour the two are the same characters, so every suggestion read as an
    unsent draft and the panel said you were the holdup when you were not.

    Dimness is the only thing separating them, so it has to survive the
    capture. Judged per line and by weight, because the "\u276f" that opens the box
    is drawn at normal brightness even when what follows is a ghost.
    """
    ghosts = set()
    for index, line in enumerate(raw.splitlines()):
        dim = False
        faint = solid = 0

        position = 0
        for match in SGR.finditer(line):
            for char in line[position:match.start()]:
                if not char.isspace() and char != "\xa0":
                    if dim:
                        faint += 1
                    else:
                        solid += 1
            position = match.end()
            for code in (match.group(1) or "0").split(";"):
                if code == "2":
                    dim = True
                elif code in ("", "0", "22"):
                    dim = False
        for char in line[position:]:
            if not char.isspace() and char != "\xa0":
                if dim:
                    faint += 1
                else:
                    solid += 1
        if faint and faint > solid:
            ghosts.add(index)
    return ghosts


def classify(screen: str, commands: list[str], raw: str = "") -> dict:
    """Return {state, label, detail, tokens, agent} for one session's screen."""
    result = {"state": UNKNOWN, "label": LABELS[UNKNOWN], "detail": "",
              "tokens": "", "agent": ""}

    agent = detect(commands)
    if commands and agent is None:
        result["state"] = SHELL
        result["label"] = LABELS[SHELL]
        result["detail"] = ", ".join(dict.fromkeys(commands))
        return result

    if not screen.strip():
        return result

    if agent is None:
        # No pane list came back, so fall back to whichever agent's chrome the
        # screen actually shows. Guessing wrong here is better than reporting
        # UNKNOWN for every session on a host where the pane probe failed.
        agent = _guess(screen)
        if agent is None:
            return result

    result["agent"] = agent.key
    result["tokens"] = agent.tokens(screen)

    # Order matters. Busy beats everything: a permission prompt from a previous
    # turn can still be on screen above a spinner that has moved on.
    if agent.BUSY and agent.BUSY.search(screen):
        result["state"] = WORKING
        result["label"] = LABELS[WORKING]
        result["detail"] = agent.busy_detail(screen)
        return result

    for pattern in agent.BLOCKED:
        if pattern.search(screen):
            result["state"] = NEEDS_YOU
            result["label"] = LABELS[NEEDS_YOU]
            result["detail"] = agent.blocked_detail(screen)
            return result

    box = agent.input_box(screen, ghost_lines(raw) if raw else set())

    # An unsent draft still wins: that one needs a human, background work does
    # not. But anything running in the background beats "idle".
    if box is not None and not box:
        background = agent.background(screen)
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
        background = agent.background(screen)
        result["detail"] = f"{box}  ({background})" if background else box
        return result

    result["state"] = READY
    result["label"] = LABELS[READY]
    result["detail"] = agent.idle_detail(screen)
    return result


def _guess(screen: str) -> Agent | None:
    """Identify the agent from its chrome, for when the pane list is missing."""
    if OpenCode.RULE.search(screen) or "Ask anything" in screen:
        return AGENTS[1]
    for agent in AGENTS:
        if agent.BUSY and agent.BUSY.search(screen):
            return agent
    return None


def needs_attention(state: str) -> bool:
    """The states where a human has to do something before anything moves."""
    return state in (NEEDS_YOU, DRAFT)
