"""Colours for the panel, taken from whatever theme the desktop is wearing.

Omarchy keeps the live theme at ~/.local/state/omarchy/current/theme, and its
colors.toml carries both a mode ("light" or "dark") and a full palette. Reading
it means this app follows the desktop theme instead of assuming a dark
terminal, and picks up the theme's own reds and greens rather than approximate
ANSI ones.

Off Omarchy, or if that file is unreadable, fall back to COLORFGBG (which most
terminals set) and then to a dark default. HELM_THEME=light|dark forces
the choice either way.

The terminal font is read the same way -- out of the config of the terminal
this machine actually has -- so a session inside helm is the size the rest of
your terminals are. HELM_FONT overrides it.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

OMARCHY_COLORS = (Path.home() / ".local" / "state" / "omarchy" /
                  "current" / "theme" / "colors.toml")

FALLBACK_DARK = {
    "mode": "dark",
    "background": "#121212", "dark_background": "#0d0d0d",
    "darker_background": "#000000", "lighter_background": "#1f1f1f",
    "foreground": "#d0d0d0", "dark_foreground": "#8a8a8a",
    "muted": "#8a8a8a", "accent": "#f5a623", "selection": "#264f78",
    "red": "#ff6b6b", "orange": "#ff8700", "yellow": "#ffd75f",
    "green": "#5faf5f", "cyan": "#5fd7d7", "blue": "#5fafff",
    "magenta": "#d787ff",
}

FALLBACK_LIGHT = {
    "mode": "light",
    "background": "#ffffff", "dark_background": "#f4f4f4",
    "darker_background": "#e6e6e6", "lighter_background": "#ededed",
    "foreground": "#1c1c1c", "dark_foreground": "#5f5f5f",
    "muted": "#6c6c6c", "accent": "#b35c00", "selection": "#cfe4ff",
    "red": "#c02020", "orange": "#a85400", "yellow": "#8a6d00",
    "green": "#0a7a4a", "cyan": "#00707a", "blue": "#0057b7",
    "magenta": "#8b008b",
}


class Palette:
    """Named colours, already resolved for the current mode.

    Themes are not obliged to define every colour -- the stock "white" theme
    has no orange or magenta, for instance -- so every lookup falls back
    through sensible neighbours and finally to the foreground."""

    def __init__(self, values: dict, source: str):
        self._values = values
        self.source = source
        self.mode = "light" if str(values.get("mode", "dark")).lower() == "light" else "dark"

    def pick(self, *names: str, default: str = "") -> str:
        for name in names:
            value = self._values.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return default

    # -- base colours -----------------------------------------------------

    @property
    def background(self) -> str:
        return self.pick("background", default="#000000" if self.mode == "dark" else "#ffffff")

    @property
    def foreground(self) -> str:
        return self.pick("foreground", default="#ffffff" if self.mode == "dark" else "#000000")

    @property
    def surface(self) -> str:
        """Dialog background: a step away from the page, whichever way that
        is -- lighter on a dark theme, darker on a light one."""
        if self.mode == "dark":
            return self.pick("lighter_background", "dark_background", default=self.background)
        return self.pick("dark_background", "darker_background", default=self.background)

    @property
    def panel(self) -> str:
        if self.mode == "dark":
            return self.pick("selection", "lighter_background", default=self.surface)
        return self.pick("darker_background", "selection", default=self.surface)

    @property
    def muted(self) -> str:
        return self.pick("muted", "dark_foreground", default=self.foreground)

    @property
    def accent(self) -> str:
        return self.pick("accent", "blue", default=self.foreground)

    # -- named hues -------------------------------------------------------

    @property
    def red(self) -> str:
        return self.pick("red", "bright_red", default=self.foreground)

    @property
    def orange(self) -> str:
        return self.pick("orange", "yellow", "bright_yellow", default=self.foreground)

    @property
    def yellow(self) -> str:
        return self.pick("yellow", "bright_yellow", "orange", default=self.foreground)

    @property
    def green(self) -> str:
        return self.pick("green", "bright_green", default=self.foreground)

    @property
    def blue(self) -> str:
        return self.pick("blue", "cyan", "accent", default=self.foreground)

    @property
    def cyan(self) -> str:
        return self.pick("cyan", "blue", default=self.foreground)

    @property
    def magenta(self) -> str:
        return self.pick("magenta", "bright_magenta", "brown", default=self.foreground)

    # -- semantic styles used by the views --------------------------------

    def priority(self, priority: int) -> str:
        """p1 shouts, p2 and p3 tint, p4 stays quiet."""
        return {4: f"bold {self.red}",
                3: self.orange,
                2: self.blue}.get(priority, self.muted)

    def due(self, state: str) -> str:
        return {"overdue": f"bold {self.red}",
                "today": f"bold {self.green}",
                "tomorrow": self.yellow,
                "week": self.foreground,
                "later": self.muted}.get(state, self.muted)

    def event(self, kind: str) -> str:
        return {"completed": f"bold {self.green}",
                "uncompleted": f"bold {self.yellow}",
                "added": self.blue,
                "updated": self.muted,
                "deleted": f"bold {self.red}",
                "moved": self.magenta}.get(kind, self.foreground)

    @property
    def heading(self) -> str:
        return f"bold underline {self.accent}"

    def signature(self) -> tuple:
        """Everything the UI actually paints with. Compared on refresh, so
        swapping between two themes of the same mode is still noticed."""
        return (self.mode, self.background, self.foreground, self.surface,
                self.panel, self.muted, self.accent, self.red, self.orange,
                self.yellow, self.green, self.blue, self.magenta)


def _read_omarchy() -> dict | None:
    try:
        with OMARCHY_COLORS.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _guess_mode() -> str:
    """COLORFGBG is 'foreground;background' in terminal colour numbers, where
    7 and 15 are the light ones. Absent on plenty of terminals, hence dark."""
    parts = [p for p in os.environ.get("COLORFGBG", "").split(";") if p.isdigit()]
    if parts:
        return "light" if int(parts[-1]) in (7, 15) else "dark"
    return "dark"


def load_palette() -> Palette:
    forced = os.environ.get("HELM_THEME", "auto").strip().lower()

    values = _read_omarchy()
    if values:
        palette = Palette(values, f"omarchy ({values.get('mode', 'dark')})")
        # a forced mode only overrides when it disagrees with the desktop
        if forced not in ("light", "dark") or forced == palette.mode:
            return palette

    mode = forced if forced in ("light", "dark") else _guess_mode()
    return Palette(FALLBACK_LIGHT if mode == "light" else FALLBACK_DARK,
                   f"built-in {mode}")


# --------------------------------------------------------------------------
# The font
# --------------------------------------------------------------------------

FALLBACK_FONT = "monospace 11"

CONFIG = Path.home() / ".config"


def _foot_font(path: Path) -> tuple[str, float] | None:
    """font=JetBrainsMono Nerd Font:size=9 -- a comma-separated list, where
    the ones after the first are only fallbacks for missing glyphs."""
    for line in _lines(path):
        if line.startswith("font="):
            first = line.split("=", 1)[1].split(",")[0]
            bits = first.split(":")
            size = next((b.split("=")[1] for b in bits[1:]
                         if b.startswith("size=")), "")
            return bits[0].strip(), _number(size)
    return None


def _ghostty_font(path: Path) -> tuple[str, float] | None:
    family, size = "", ""
    for line in _lines(path):
        key, _, value = line.partition("=")
        if key.strip() == "font-family":
            family = value.strip().strip('"')
        elif key.strip() == "font-size":
            size = value.strip()
    return (family, _number(size)) if family else None


def _kitty_font(path: Path) -> tuple[str, float] | None:
    family, size = "", ""
    for line in _lines(path):
        if line.startswith("font_family "):
            family = line[len("font_family "):].strip()
        elif line.startswith("font_size "):
            size = line[len("font_size "):].strip()
    return (family, _number(size)) if family else None


def _alacritty_font(path: Path) -> tuple[str, float] | None:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    font = data.get("font") or {}
    family = (font.get("normal") or {}).get("family", "")
    return (family, _number(str(font.get("size", "")))) if family else None


# In order: the first one whose config is readable *and* whose binary is
# installed wins. Reading the config of a terminal that is not on the machine
# would be following someone else's setup.
TERMINALS = (
    ("foot", CONFIG / "foot" / "foot.ini", _foot_font),
    ("ghostty", CONFIG / "ghostty" / "config", _ghostty_font),
    ("alacritty", CONFIG / "alacritty" / "alacritty.toml", _alacritty_font),
    ("kitty", CONFIG / "kitty" / "kitty.conf", _kitty_font),
)


def _lines(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _number(text: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else 0.0


def terminal_font(which=None) -> str:
    """A Pango font string for the session terminals.

    HELM_FONT wins ("JetBrainsMono Nerd Font 10"). Otherwise this machine's
    terminal is asked what it uses, so helm matches the terminals beside it
    rather than picking a size of its own. `which` is for the tests.
    """
    forced = os.environ.get("HELM_FONT", "").strip()
    if forced:
        return forced

    for binary, path, parse in (which or TERMINALS):
        if not path.exists() or not shutil.which(binary):
            continue
        found = parse(path)
        if not found or not found[0]:
            continue
        family, size = found
        return f"{family} {size:g}" if size else family

    return FALLBACK_FONT
