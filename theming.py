"""Colours for the panel, taken from whatever theme the desktop is wearing.

Omarchy keeps the live theme at ~/.local/state/omarchy/current/theme, and its
colors.toml carries both a mode ("light" or "dark") and a full palette. Reading
it means this app follows the desktop theme instead of assuming a dark
terminal, and picks up the theme's own reds and greens rather than approximate
ANSI ones.

Off Omarchy, or if that file is unreadable, fall back to COLORFGBG (which most
terminals set) and then to a dark default. CONN_THEME=light|dark forces
the choice either way.

The terminal font is read the same way -- out of the config of the terminal
this machine actually has -- so a session inside conn is the size the rest of
your terminals are. CONN_FONT overrides it.

Everything here is read once, at startup: conn does not notice a theme or
font changed mid-run. Restarting picks the change up, and the sessions are
tmux, so a restart costs nothing but the views.

A theme's colours are not all text colours, and taking one for the other is
how conn once drew half its sidebar invisibly. Omarchy's `muted` is a border
shade -- its own templates call it "subtle" and never write words in it --
and on Nord it sat 1.2:1 against the sidebar, on Last Horizon 1.0:1. So every
colour conn writes text in is held to LEGIBLE against every surface it lands
on. A colour that already clears it is used exactly as the theme has it; one
that does not is made lighter (or darker) in its own hue only as far as it has to go.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import tomllib
from functools import cached_property
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


# WCAG AA for body text. Everything conn writes is body-sized or smaller.
LEGIBLE = 4.5

# What GTK's own theme draws under the widgets conn does not restyle --
# popover contents and lists, a framed button's lighter end, a hovered flat
# button -- from GTK 4.22's built-in Default-dark.css and Default-light.css.
# conn's text classes land on these in the ? guide, the menus and the dialogs,
# so they are measured too. Omarchy gives GTK the dark variant with a dark
# theme and the light one with a light theme.
GTK_SURFACES = {
    "dark": ("#2d2d2d", "#3a3a3a", "#373737"),
    "light": ("#ffffff", "#f6f5f4", "#dad6d2"),
}

# WCAG's floor for a graphical object that carries meaning, which is what the
# state colours are: a mark, and the one-line tally beside the wordmark. Held
# to LEGIBLE instead, Nord's red on its own sidebar can only be a pink, and a
# "needs you" that does not look red is worse than one a shade dimmer.
SIGNAL = 3.0

# How far toward the background Omarchy's own templates mix the foreground for
# secondary text (pi's mutedText, the shell's placeholder).
DIM = 0.34


HEX = re.compile(r"#?([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


def _rgb(colour: str) -> tuple[int, int, int] | None:
    """#rgb, #rgba, #rrggbb or #rrggbbaa. Anything else -- rgb(), a name -- is
    None, and a colour conn cannot measure is left exactly as the theme wrote
    it. Matched whole, because int(..., 16) alone takes "-1" and " f"."""
    found = HEX.fullmatch(colour.strip())
    if found is None:
        return None
    text = found.group(1)
    if len(text) in (3, 4):
        text = "".join(c * 2 for c in text[:3])
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def mix(start: str, end: str, amount: float) -> str:
    """start moved `amount` of the way to end -- the same arithmetic as
    Omarchy's `{{ mix start end amount }}`. Unmeasurable input comes back as
    start."""
    a, b = _rgb(start), _rgb(end)
    if a is None or b is None:
        return start
    amount = min(max(amount, 0.0), 1.0)
    return "#" + "".join(f"{int(x * (1 - amount) + y * amount + 0.5):02x}"
                         for x, y in zip(a, b))


def _linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _gamma(value: float) -> float:
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def luminance(colour: str) -> float | None:
    rgb = _rgb(colour)
    if rgb is None:
        return None
    r, g, b = (_linear(v / 255) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, other: str) -> float | None:
    """The WCAG ratio, 1 to 21. None when either side cannot be measured."""
    a, b = luminance(one), luminance(other)
    if a is None or b is None:
        return None
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def _oklch(colour: str) -> tuple[float, float, float] | None:
    """Lightness, chroma, hue -- the space where raising lightness leaves a
    red looking red. Björn Ottosson's OKLab, in polar form."""
    rgb = _rgb(colour)
    if rgb is None:
        return None
    r, g, b = (_linear(v / 255) for v in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    lightness = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return lightness, math.hypot(a, bb), math.atan2(bb, a)


def _from_oklch(lightness: float, chroma: float, hue: float) -> str:
    """Back to #rrggbb. A lightness sRGB cannot show at this chroma gives up
    chroma, not lightness -- lightness is what the contrast is made of."""
    def channels(c: float) -> tuple[float, float, float]:
        a, b = c * math.cos(hue), c * math.sin(hue)
        l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
        return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    rgb = channels(chroma)
    if not all(-1e-6 <= v <= 1 + 1e-6 for v in rgb):
        low, high = 0.0, chroma
        for _ in range(20):
            mid = (low + high) / 2
            if all(-1e-6 <= v <= 1 + 1e-6 for v in channels(mid)):
                low = mid
            else:
                high = mid
        rgb = channels(low)
    return "#" + "".join(f"{round(_gamma(min(max(v, 0.0), 1.0)) * 255):02x}"
                         for v in rgb)


def legible(colour: str, surfaces: tuple[str, ...],
            minimum: float = LEGIBLE) -> str:
    """colour, unless it fails `minimum` on one of `surfaces`; then the same
    hue made lighter or darker by the least that clears every one. Where no
    lightness can, the one that comes closest -- which is never worse than the
    colour it started from. A colour or surface that cannot be measured is
    left alone: a guess would be worse than the theme."""
    def worst(candidate: str) -> float | None:
        ratios = [contrast(candidate, s) for s in surfaces]
        return None if None in ratios else min(ratios)

    here = worst(colour)
    polar = _oklch(colour)
    if here is None or here >= minimum or polar is None:
        return colour
    lightness, chroma, hue = polar
    passing: list[tuple[float, str]] = []
    closest = (here, colour)
    # Both ways. Surfaces either side of the middle -- a dark page with a
    # light panel -- can make the direction with more contrast in principle
    # the one with nothing that passes. Fifty steps each is a 2% resolution,
    # finer than an eye separates two text colours.
    for target in (1.0, 0.0):
        for step in range(1, 51):
            moved = lightness + (target - lightness) * step / 50
            candidate = _from_oklch(moved, chroma, hue)
            ratio = worst(candidate) or 0
            if ratio >= minimum:
                passing.append((abs(moved - lightness), candidate))
                break
            if ratio > closest[0]:
                closest = (ratio, candidate)
    if passing:
        return min(passing)[1]
    return closest[1]


class Palette:
    """Named colours, already resolved for the current mode.

    Themes are not obliged to define every colour -- the stock "white" theme
    has no orange or magenta, for instance -- so every lookup falls back
    through sensible neighbours and finally to the foreground.

    The names conn writes in come back readable on every one of `surfaces`:
    muted and accent to LEGIBLE, the hues to SIGNAL. The raw theme value of
    any of them is still there through pick()."""

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

    def _text(self, colour: str, minimum: float = LEGIBLE) -> str:
        return legible(colour, self.surfaces, minimum)

    @property
    def surfaces(self) -> tuple[str, ...]:
        """Everything conn's text is drawn on: its own sidebar and page, and
        the GTK surfaces under its popovers, lists and buttons."""
        return (self.panel, self.background) + GTK_SURFACES[self.mode]

    # -- base colours -----------------------------------------------------

    @property
    def background(self) -> str:
        # bg and color0 are how themes older than the semantic palette say it
        return self.pick("background", "bg", "color0",
                         default="#000000" if self.mode == "dark" else "#ffffff")

    @property
    def foreground(self) -> str:
        return self.pick("foreground", "fg", "color7",
                         default="#ffffff" if self.mode == "dark" else "#000000")

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

    @cached_property
    def muted(self) -> str:
        """Secondary text: host headings, details, the footer. Omarchy's own
        recipe for it, not the theme's `muted`, which is a border shade."""
        return self._text(mix(self.foreground, self.background, DIM))

    @cached_property
    def accent(self) -> str:
        return self._text(self.pick("accent", "blue", default=self.foreground))

    @cached_property
    def on_accent(self) -> str:
        """Text on an accent-filled row: whichever end of the theme reads
        better on it."""
        accent = self.accent
        return max((self.background, self.foreground),
                   key=lambda c: contrast(c, accent) or 0)

    # -- named hues -------------------------------------------------------

    @cached_property
    def red(self) -> str:
        return self._text(self.pick("red", "bright_red", default=self.foreground), SIGNAL)

    @cached_property
    def orange(self) -> str:
        return self._text(self.pick("orange", "yellow", "bright_yellow",
                                    default=self.foreground), SIGNAL)

    @cached_property
    def yellow(self) -> str:
        return self._text(self.pick("yellow", "bright_yellow", "orange",
                                    default=self.foreground), SIGNAL)

    @cached_property
    def green(self) -> str:
        return self._text(self.pick("green", "bright_green", default=self.foreground), SIGNAL)

    @cached_property
    def blue(self) -> str:
        return self._text(self.pick("blue", "cyan", "accent", default=self.foreground), SIGNAL)

    @cached_property
    def cyan(self) -> str:
        return self._text(self.pick("cyan", "blue", default=self.foreground), SIGNAL)

    @cached_property
    def magenta(self) -> str:
        return self._text(self.pick("magenta", "bright_magenta", "brown",
                                    default=self.foreground), SIGNAL)

    # -- the terminal -----------------------------------------------------

    @cached_property
    def terminal(self) -> list[str]:
        """The sixteen ANSI colours, filled the way Omarchy's foot.ini.tpl and
        alacritty.toml.tpl fill them, so a program's "blue" in a session is
        the blue it is in every other terminal. Without
        this VTE uses its own built-in palette, and tmux's status line came up
        in a navy nobody's theme contains.

        Taken as the theme has them, with one exception. Slot 8, bright black,
        is where Omarchy puts `muted` -- the border shade -- and slot 8 is
        what tmux, shells and prompts write their dim *text* in. It is lifted
        to LEGIBLE on the background when it falls short; the other fifteen
        are also drawn as backgrounds, where lifting them would cost the text
        on top."""
        p = self.pick
        fg, bg = self.foreground, self.background
        # The name first and the number second, as omarchy-theme-color
        # resolves it and foot.ini.tpl reads it (regular1={{ red }}); the
        # brights derive from the named colour too.
        red = p("red", "color1", default=fg)
        green = p("green", "color2", default=fg)
        yellow = p("yellow", "color3", default=fg)
        blue = p("blue", "color4", default=fg)
        magenta = p("magenta", "color5", "purple", default=fg)
        cyan = p("cyan", "color6", default=fg)
        dim = p("muted", "color8", "dark_foreground", "dark_fg", default=fg)
        return [
            bg, red, green, yellow, blue, magenta, cyan, fg,
            legible(dim, (bg,)),
            p("bright_red", "color9", default=mix(red, "#ffffff", 0.2)),
            p("bright_green", "color10", default=mix(green, "#ffffff", 0.2)),
            p("bright_yellow", "color11", default=mix(yellow, "#ffffff", 0.2)),
            p("bright_blue", "color12", default=mix(blue, "#ffffff", 0.2)),
            p("bright_magenta", "bright_purple", "color13",
              default=mix(magenta, "#ffffff", 0.2)),
            p("bright_cyan", "color14", default=mix(cyan, "#ffffff", 0.2)),
            p("bright_foreground", "bright_fg", "color15", default=fg),
        ]


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
    forced = os.environ.get("CONN_THEME", "auto").strip().lower()

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

    CONN_FONT wins ("JetBrainsMono Nerd Font 10"). Otherwise this machine's
    terminal is asked what it uses, so conn matches the terminals beside it
    rather than picking a size of its own. `which` is for the tests.
    """
    forced = os.environ.get("CONN_FONT", "").strip()
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
