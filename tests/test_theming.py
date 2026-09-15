#!/usr/bin/env python3
"""The colours, checked against every theme there is to wear.

Run it with plain python -- no display, no test dependencies:

    python3 tests/test_theming.py

Nord and Last Horizon are written out here, because they are the two that
showed the bug: Omarchy's `muted` is a border shade, and conn wrote its
sidebar in it -- 1.2:1 on Nord, 1.0:1 on Last Horizon, which is to say not
at all. Every stock theme installed on this machine is checked on top of
those, and where Omarchy's own resolver is on the PATH the terminal palette
is checked against what it hands foot. Off Omarchy those parts skip.
"""

from __future__ import annotations

import glob
import math
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import theming as T  # noqa: E402

# /usr/share/omarchy/themes/nord/colors.toml, as shipped.
NORD = {
    "mode": "dark", "accent": "#81a1c1", "selection": "#434c5e",
    "muted": "#4c566a", "background": "#2e3440", "dark_background": "#222730",
    "darker_background": "#191c23", "lighter_background": "#3b4252",
    "foreground": "#d8dee9", "dark_foreground": "#667080",
    "light_foreground": "#adb5c4", "bright_foreground": "#d8dee9",
    "red": "#bf616a", "yellow": "#ebcb8b", "orange": "#d5967a",
    "green": "#a3be8c", "cyan": "#88c0d0", "blue": "#81a1c1",
    "magenta": "#b48ead", "brown": "#6a4b3d", "bright_red": "#bf616a",
    "bright_yellow": "#ebcb8b", "bright_green": "#a3be8c",
    "bright_cyan": "#8fbcbb", "bright_blue": "#81a1c1",
    "bright_magenta": "#b48ead",
}

# The parts of /usr/share/omarchy/themes/last-horizon/colors.toml that matter:
# its muted *is* its selection, so the old sidebar text was the sidebar.
LAST_HORIZON = {
    "mode": "dark", "accent": "#b59790", "selection": "#584e51",
    "muted": "#584e51", "background": "#0c0b0c", "dark_background": "#090809",
    "darker_background": "#060606", "lighter_background": "#0c0b0c",
    "foreground": "#FAFCFB", "red": "#c38b7b", "yellow": "#6B5E73",
    "green": "#87a9b0",
}

# And Flexoki Light's, a light theme whose yellow is 1.8:1 on its own panel.
LIGHT = {
    "mode": "light", "accent": "#205EA6", "selection": "#CECDC3",
    "muted": "#B7B5AC", "background": "#FFFCF0", "dark_background": "#f2efe4",
    "darker_background": "#e5e2d8", "lighter_background": "#E6E4D9",
    "foreground": "#100F0F", "red": "#D14D41", "yellow": "#D0A215",
    "green": "#879A39",
}


def worst(colour: str, palette: T.Palette) -> float:
    return min(T.contrast(colour, s) for s in palette.surfaces)


def readable(palette: T.Palette) -> list[tuple[str, bool, str]]:
    """What has to hold for any theme at all."""
    out = []
    for name in ("muted", "accent"):
        ratio = worst(getattr(palette, name), palette)
        out.append((f"{name} reads as text", ratio >= T.LEGIBLE - 0.01,
                    f"{ratio:.2f}"))
    for name in ("red", "yellow", "green", "orange", "blue", "cyan", "magenta"):
        ratio = worst(getattr(palette, name), palette)
        out.append((f"{name} reads as a signal", ratio >= T.SIGNAL - 0.01,
                    f"{ratio:.2f}"))
    ratio = T.contrast(palette.on_accent, palette.accent)
    out.append(("a selected row reads", ratio >= T.LEGIBLE - 0.01, f"{ratio:.2f}"))
    term = palette.terminal
    out.append(("the terminal gets all sixteen", len(term) == 16
                and all(T._rgb(c) for c in term), f"{term}"))
    ratio = T.contrast(term[8], palette.background)
    out.append(("and its dim text reads", ratio >= T.LEGIBLE - 0.01, f"{ratio:.2f}"))
    return out


def hue_shift(before: str, after: str) -> float:
    """Degrees round the OKLCH wheel between two colours."""
    a, b = T._oklch(before)[2], T._oklch(after)[2]
    return abs(math.degrees(math.atan2(math.sin(a - b), math.cos(a - b))))


def main() -> int:
    failures = 0
    total = 0

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal failures, total
        total += 1
        failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {label}"
              + (f"   {detail}" if detail and not passed else ""))

    # -- the two that were broken ----------------------------------------
    nord = T.Palette(NORD, "nord")
    check("nord's raw muted was the bug",
          T.contrast(NORD["muted"], nord.panel) < 1.5,
          f"{T.contrast(NORD['muted'], nord.panel):.2f}")
    for label, passed, detail in readable(nord):
        check(f"nord: {label}", passed, detail)
    horizon = T.Palette(LAST_HORIZON, "last-horizon")
    check("last horizon's muted is its own panel",
          LAST_HORIZON["muted"] == horizon.panel)
    for label, passed, detail in readable(horizon):
        check(f"last horizon: {label}", passed, detail)
    light = T.Palette(LIGHT, "light")
    for label, passed, detail in readable(light):
        check(f"flexoki light: {label}", passed, detail)
    check("a light theme's colours are darkened, not lightened",
          T.luminance(light.yellow) < T.luminance(LIGHT["yellow"]))

    # -- only as much as it takes ----------------------------------------
    check("a colour that already reads is the theme's own",
          nord.yellow == NORD["yellow"] and nord.green == NORD["green"],
          f"{nord.yellow} {nord.green}")
    check("so is every terminal slot but bright black",
          nord.terminal[:8] == [NORD["background"], NORD["red"], NORD["green"],
                                NORD["yellow"], NORD["blue"], NORD["magenta"],
                                NORD["cyan"], NORD["foreground"]])
    check("a lifted red is still red", hue_shift(NORD["red"], nord.red) < 6,
          f"{NORD['red']} -> {nord.red}, {hue_shift(NORD['red'], nord.red):.1f} deg")
    check("and lifted only to the floor, not past it",
          worst(nord.red, nord) < T.SIGNAL + 0.25, f"{worst(nord.red, nord):.2f}")
    check("dim text is dimmer than the foreground",
          worst(nord.muted, nord) < worst(nord.foreground, nord))

    # -- what cannot be measured is left alone ---------------------------
    check("a colour that is not hex passes through",
          T.legible("rgb(1, 2, 3)", ("#000000",)) == "rgb(1, 2, 3)")
    check("so does one on a surface that is not hex",
          T.legible("#111111", ("black",)) == "#111111")
    check("mixing with nonsense gives back the start",
          T.mix("#123456", "nonsense", 0.5) == "#123456")
    check("short hex is read", T._rgb("#fff") == (255, 255, 255)
          and T._rgb("#11223344") == (0x11, 0x22, 0x33))
    # int(..., 16) takes a sign and spaces; "#-10000" once reached the cube
    # root as a negative, came back complex, and took the palette down.
    check("hex that only int() would accept is not a colour",
          all(T._rgb(bad) is None for bad in ("#-10000", "# ff ff", "#ff ff f",
                                               "#+fffff", "#１２３４５６", "")))
    check("and passes through instead of raising",
          T.legible("#-10000", ("#434c5e",)) == "#-10000")

    # -- surfaces either side of the middle ------------------------------
    # A dark page with a light panel: the brighter direction can hold nothing
    # that passes while the darker one does, and where nothing passes, the
    # extreme can read worse than what the theme gave.
    seed, never_worse, reached = 12345, True, True
    for _ in range(3000):
        values = []
        for _ in range(3):
            seed = (seed * 1103515245 + 12345) % 2**31
            values.append(f"#{seed % 0xffffff:06x}")
        colour, *surfaces = values
        for minimum in (T.SIGNAL, T.LEGIBLE):
            before = min(T.contrast(colour, s) for s in surfaces)
            got = T.legible(colour, tuple(surfaces), minimum)
            after = min(T.contrast(got, s) for s in surfaces)
            never_worse &= after >= before - 1e-9
            # a grey scan says whether the floor was reachable at all
            best = max(min(T.contrast(f"#{v:02x}{v:02x}{v:02x}", s)
                           for s in surfaces) for v in range(256))
            reached &= after >= minimum or best < minimum + 0.2
    check("a lifted colour never reads worse than the theme's", never_worse)
    check("and a floor that can be reached is", reached)
    got = T.legible("#2c11b6", ("#9febc2", "#362b14"), T.SIGNAL)
    check("including the way the brighter end would miss",
          min(T.contrast(got, s) for s in ("#9febc2", "#362b14")) >= T.SIGNAL,
          got)
    check("the oklch round trip is exact on the theme's colours",
          all(T._from_oklch(*T._oklch(c)).lower() == c.lower()
              for c in NORD.values() if c.startswith("#")))
    check("the built-in palettes read too",
          all(passed for _, passed, _ in
              readable(T.Palette(T.FALLBACK_DARK, "dark"))
              + readable(T.Palette(T.FALLBACK_LIGHT, "light"))))

    # -- the name outranks the number, as it does for foot ---------------
    both = T.Palette({"mode": "dark", "background": "#101010",
                      "foreground": "#eeeeee", "red": "#f7768e",
                      "color1": "#aa0000", "color9": "#ff5555",
                      "bright_foreground": "#fafafa", "color15": "#eeeeee"},
                     "both")
    check("a theme with red and color1 gets its red",
          both.terminal[1] == "#f7768e", both.terminal[1])
    check("and bright red derives from it when it has no bright_red",
          both.terminal[9] == "#ff5555")
    no_bright = T.Palette({"mode": "dark", "background": "#101010",
                           "foreground": "#eeeeee", "red": "#f7768e",
                           "color1": "#aa0000"}, "no-bright")
    check("a missing bright red is mixed from the named red",
          no_bright.terminal[9] == T.mix("#f7768e", "#ffffff", 0.2),
          no_bright.terminal[9])
    check("bright white is bright_foreground before color15",
          both.terminal[15] == "#fafafa", both.terminal[15])

    # -- Omarchy's own arithmetic ----------------------------------------
    # claude.json.tpl: inactive = {{ mix foreground background 40% }}, which
    # Omarchy baked to #949aa5 for Nord.
    check("mix is Omarchy's mix",
          T.mix(NORD["foreground"], NORD["background"], 0.40) == "#949aa5",
          T.mix(NORD["foreground"], NORD["background"], 0.40))

    # -- every theme on this machine -------------------------------------
    themes = sorted(glob.glob("/usr/share/omarchy/themes/*/colors.toml")
                    + glob.glob(str(Path.home() / ".config/omarchy/themes/*/colors.toml")))
    resolver = shutil.which("omarchy-theme-color")
    # Which palette key foot's template puts in each slot: regular1={{ red_strip }}.
    template = Path("/usr/share/omarchy/default/themed/foot.ini.tpl")
    slot_keys = {}
    if template.exists():
        for line in template.read_text().splitlines():
            key, _, value = line.partition("=")
            for prefix, base in (("regular", 0), ("bright", 8)):
                if key.startswith(prefix) and key[len(prefix):].isdigit():
                    slot_keys[base + int(key[len(prefix):])] = (
                        value.strip().strip("{} ").removesuffix("_strip"))
    if len(slot_keys) != 16:
        resolver = None
    if not themes:
        print("skip no Omarchy themes installed")
    for path in themes:
        name = Path(path).parent.name
        with open(path, "rb") as handle:
            palette = T.Palette(tomllib.load(handle), name)
        bad = [f"{label} ({detail})" for label, passed, detail in readable(palette)
               if not passed]
        check(f"{name}: everything reads", not bad, "; ".join(bad))
        if not resolver:
            continue
        done = subprocess.run([resolver, "--file", path, "--all"],
                              capture_output=True, text=True)
        resolved = dict(line.split("\t", 1) for line in done.stdout.splitlines()
                        if "\t" in line)
        theirs = [resolved.get(slot_keys[i], "").lower() for i in range(16)]
        ours = [c.lower() for c in palette.terminal]
        differs = [i for i in range(16) if i != 8 and theirs[i] != ours[i]]
        check(f"{name}: the terminal is the one foot gets", not differs,
              ", ".join(f"slot {i} {ours[i]} vs {slot_keys[i]} {theirs[i]}"
                        for i in differs))
        eight = T.contrast(theirs[8], palette.background) if theirs[8] else None
        if eight is not None and eight >= T.LEGIBLE:
            check(f"{name}: bright black is untouched when it reads",
                  ours[8] == theirs[8], f"{ours[8]} vs {theirs[8]}")
    if themes and not resolver:
        print("skip omarchy-theme-color is not on the PATH")

    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
