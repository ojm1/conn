"""Host inventory and remote probing for the server panel.

Everything that touches ~/.ssh/config, sshfs or a remote box lives here, so
tui.py only ever deals with plain dicts. Nothing in this module blocks for
longer than PROBE_TIMEOUT -- the UI runs it on worker threads and would
otherwise freeze on a box that is powered off.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import agent_state

SSH_CONFIG = Path.home() / ".ssh" / "config"
MNT_ROOT = Path.home() / "mnt"

CONNECT_TIMEOUT = 6      # seconds to get a TCP + auth handshake
CAPTURE_LINES = 200     # lines of each remote screen to bring back;
                        # the panel renders as many as it can fit
PROBE_TIMEOUT = 15       # hard ceiling on the whole probe


class HostError(RuntimeError):
    """Something the user needs to read, not a traceback."""


# ---------------------------------------------------------------------------
# ~/.ssh/config
# ---------------------------------------------------------------------------

def list_hosts() -> list[str]:
    """Host aliases in the order they appear in the config.

    File order beats alphabetical here -- people group related boxes together,
    and that grouping is information the panel should preserve.
    """
    try:
        text = SSH_CONFIG.read_text()
    except OSError:
        return []

    names: list[str] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            for name in parts[1:]:
                if not re.search(r"[*?!]", name) and name not in names:
                    names.append(name)
    return names


def resolve(host: str) -> dict:
    """Ask ssh itself what a name expands to, rather than re-parsing config.

    This picks up Match blocks, Include files and the Host * defaults, so the
    panel shows the connection that will actually be made.
    """
    info = {"user": "", "hostname": "", "port": "", "identityfile": ""}
    try:
        out = subprocess.run(["ssh", "-G", host], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return info

    for line in out.splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        if key in info and value and not info[key]:
            info[key] = value

    # Fall back only where ssh told us nothing, so a real HostName always wins
    # over the alias we were asked about.
    info["hostname"] = info["hostname"] or host
    info["port"] = info["port"] or "22"
    return info


def target(info: dict) -> str:
    port = info.get("port", "22")
    suffix = "" if port == "22" else f":{port}"
    user = info.get("user", "")
    return f"{user}@{info.get('hostname', '')}{suffix}" if user else f"{info.get('hostname','')}{suffix}"


def backup_config() -> Path:
    """Copy the config aside before touching it. Cheap, and it means an edit
    that goes wrong is a one-line recovery rather than a retype."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = SSH_CONFIG.with_suffix(f".bak.{stamp}")
    shutil.copy2(SSH_CONFIG, dest)
    return dest


def add_host(name: str, hostname: str, user: str, port: str = "22") -> Path:
    """Insert a new Host block above the Host * defaults.

    Order matters to ssh: it is first-match-wins, so a block placed after
    Host * would have the defaults applied before its own settings.
    """
    name = name.strip()
    hostname = hostname.strip()
    user = user.strip()
    port = (port or "22").strip()

    if not name or re.search(r"[*?!\s]", name):
        raise HostError("Name must be a single word with no * ? or spaces.")
    if not hostname:
        raise HostError("Hostname or IP is required.")
    if name in list_hosts():
        raise HostError(f"'{name}' is already in ~/.ssh/config.")
    if not port.isdigit():
        raise HostError("Port must be a number.")

    block = [f"Host {name}", f"    HostName {hostname}"]
    if user:
        block.append(f"    User {user}")
    if port != "22":
        block.append(f"    Port {port}")

    backup = backup_config()
    lines = SSH_CONFIG.read_text().splitlines()

    # Find the wildcard defaults block and sit just above it, keeping the
    # blank line that precedes it for readability.
    insert_at = len(lines)
    for index, line in enumerate(lines):
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host" and any("*" in p for p in parts[1:]):
            insert_at = index
            # Walk back over the comment banner that introduces the defaults
            # block; it belongs to Host *, so new hosts go above it, not
            # underneath a heading that says "keep this last".
            while insert_at > 0:
                above = lines[insert_at - 1].strip()
                if above and not above.startswith("#"):
                    break
                insert_at -= 1
            break

    merged = lines[:insert_at] + [""] + block + lines[insert_at:]
    SSH_CONFIG.write_text("\n".join(merged).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


# ---------------------------------------------------------------------------
# Probing a live host
# ---------------------------------------------------------------------------

# One round trip collects everything the panel shows. Each section is opened
# by a ###MARKER so the parse below cannot be confused by a value that happens
# to contain a newline.
REMOTE_PROBE = r"""
echo "###UPTIME"; uptime -p 2>/dev/null || uptime 2>/dev/null
echo "###LOAD";   cut -d' ' -f1-3 /proc/loadavg 2>/dev/null
echo "###CPUS";   nproc 2>/dev/null
echo "###MEM";    free -m 2>/dev/null | awk '/^Mem:/{print $3" "$2}'
echo "###DISK";   df -h / 2>/dev/null | tail -1 | awk '{print $3" "$2" "$5}'
echo "###TMUX";   tmux list-sessions -F "#{session_name}|#{session_windows}|#{?session_attached,attached,detached}|#{session_activity}" 2>/dev/null
echo "###PANES";  tmux list-panes -a -F "#{session_name}|#{pane_current_command}|#{pane_current_path}" 2>/dev/null
echo "###CAPTURE"
tmux list-sessions -F "#{session_name}" 2>/dev/null | while read -r s; do
  echo "@@@SESSION:$s"
  tmux capture-pane -p -t "$s" 2>/dev/null | tail -__CAPLINES__
done
echo "###END"
"""

SECTIONS = ("UPTIME", "LOAD", "CPUS", "MEM", "DISK", "TMUX", "PANES", "CAPTURE", "END")


def _split_captures(text: str) -> dict[str, str]:
    """Pull the per-session screen grabs out of the raw probe output.

    Unlike every other section these must survive verbatim: blank lines and
    leading spaces are part of what makes a terminal screen readable, and the
    state classifier reads them.
    """
    start = text.find("###CAPTURE")
    if start < 0:
        return {}
    body = text[start + len("###CAPTURE"):]
    end = body.find("###END")
    if end >= 0:
        body = body[:end]

    screens: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        if line.startswith("@@@SESSION:"):
            current = line[len("@@@SESSION:"):].strip()
            screens[current] = []
        elif current is not None:
            screens[current].append(line.rstrip())
    return {name: "\n".join(lines).strip("\n") for name, lines in screens.items()}


def _split_sections(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current = None
    for line in text.split("###CAPTURE")[0].splitlines():
        stripped = line.strip()
        if stripped.startswith("###") and stripped[3:] in found:
            current = stripped[3:]
            continue
        if current and stripped:
            found[current].append(stripped)
    return found


def blank(host: str) -> dict:
    """The row shape, before anything is known about the host."""
    return {
        "host": host, "state": "unknown", "target": "", "error": "",
        "uptime": "", "load": "", "cpus": "", "mem": "", "disk": "",
        "sessions": [], "checked": 0.0, "mounted": False,
    }


def probe(host: str) -> dict:
    """Query one host. Never raises -- failure is a state, not an exception,
    because a dashboard that crashes on an offline box is useless."""
    row = blank(host)
    row["target"] = target(resolve(host))
    row["mounted"] = is_mounted(host)
    row["checked"] = time.time()

    try:
        done = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
             "-o", "StrictHostKeyChecking=accept-new",
             host, REMOTE_PROBE.replace("__CAPLINES__", str(CAPTURE_LINES))],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        row["state"] = "down"
        row["error"] = "timed out"
        return row
    except OSError as exc:
        row["state"] = "down"
        row["error"] = str(exc)
        return row

    if "###END" not in done.stdout:
        stderr = (done.stderr or "").strip().splitlines()
        last = stderr[-1] if stderr else "unreachable"
        # Reaching the box but being refused is a different problem from the
        # box being off, and it has a specific fix, so it gets its own state.
        row["state"] = "nokey" if "Permission denied" in last else "down"
        row["error"] = last
        return row

    parts = _split_sections(done.stdout)
    row["state"] = "up"
    row["uptime"] = _first(parts["UPTIME"]).removeprefix("up ")
    row["load"] = _first(parts["LOAD"])
    row["cpus"] = _first(parts["CPUS"])

    mem = _first(parts["MEM"]).split()
    if len(mem) == 2:
        row["mem"] = f"{_gib(mem[0])} / {_gib(mem[1])}"

    disk = _first(parts["DISK"]).split()
    if len(disk) == 3:
        row["disk"] = f"{disk[0]} / {disk[1]} ({disk[2]})"

    panes: dict[str, list[dict]] = {}
    for line in parts["PANES"]:
        bits = line.split("|")
        if len(bits) == 3:
            panes.setdefault(bits[0], []).append({"cmd": bits[1], "path": bits[2]})

    screens = _split_captures(done.stdout)
    for line in parts["TMUX"]:
        bits = line.split("|")
        if len(bits) < 3:
            continue
        name = bits[0]
        screen = screens.get(name, "")
        commands = [pane["cmd"] for pane in panes.get(name, [])]
        row["sessions"].append({
            "name": name, "windows": bits[1],
            "attached": bits[2] == "attached",
            "activity": int(bits[3]) if len(bits) > 3 and bits[3].isdigit() else 0,
            "panes": panes.get(name, []),
            "screen": screen,
            "agent": agent_state.classify(screen, commands),
        })
    return row


def _first(lines: list[str]) -> str:
    return lines[0] if lines else ""


def _gib(megabytes: str) -> str:
    try:
        value = int(megabytes)
    except ValueError:
        return megabytes
    return f"{value/1024:.1f}G" if value >= 1024 else f"{value}M"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def is_mounted(host: str) -> bool:
    try:
        return os.path.ismount(MNT_ROOT / host)
    except OSError:
        return False


def mount(host: str) -> str:
    done = subprocess.run(["ssh-mount", host], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=40)
    if done.returncode != 0:
        raise HostError((done.stderr or done.stdout or "sshfs failed").strip().splitlines()[-1])
    return str(MNT_ROOT / host)


def unmount(host: str) -> None:
    done = subprocess.run(["ssh-mount", "-u", host], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        raise HostError((done.stderr or done.stdout or "unmount failed").strip().splitlines()[-1])


def open_files(path: str) -> None:
    subprocess.Popen(["xdg-open", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


SESSION_APP_ID = "org.omarchy.helm-session"
PANEL_APP_ID = "org.omarchy.helm"


# NOTE: no hyprctl window manipulation here on purpose. On this setup every
# `hyprctl dispatch` that takes an argument fails ("hl.dispatch(...)" Lua parse
# error), so focus/move calls look like they work and silently do nothing.
# Window placement is left to the compositor's own rules.


def launch(argv: list[str], app_id: str = SESSION_APP_ID) -> None:
    """Open a terminal window running argv, detached from this process."""
    if shutil.which("omarchy-launch-tui"):
        command = ["omarchy-launch-tui", f"--app-id={app_id}"] + argv
    else:
        command = ["foot", "-a", app_id] + argv
    subprocess.Popen(command, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# Where ssh-connect leaves a note when a window died on the way up. Watching the
# launcher's own exit would tell us nothing: omarchy-launch-tui is `exec setsid
# uwsm-app ...`, so it is gone within milliseconds whether the session came up
# or not, and the real error is on the far side of a terminal we do not own.
LAUNCH_ERROR = Path(os.environ.get("XDG_CACHE_HOME",
                                   Path.home() / ".cache")) / "helm" / "last-launch-error"


def launch_error(since: float) -> str:
    """The failure a just-launched window reported, or "" if it is running.

    `since` is when the launch started, so an old note from yesterday's failure
    cannot be mistaken for this one.
    """
    try:
        stamp, target, reason = LAUNCH_ERROR.read_text().strip().split("\t", 2)
        # `date +%s` truncates to the second, so a note written 200ms after the
        # launch can carry a stamp a fraction *before* it. Without the second
        # of slack, the fastest failures -- the ones worth reporting -- would
        # be the ones dismissed as stale.
        if float(stamp) < since - 1:
            return ""
    except (OSError, ValueError):
        return ""
    return f"{target}: {reason}"


def wait_launch_error(since: float, timeout: float = 6.0) -> str:
    """Give a launch long enough to fail, then say how. A window that is still
    up when the time is out has not failed, so this returns "".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        error = launch_error(since)
        if error:
            return error
        time.sleep(0.25)
    return ""


# A long-lived loop on the far side that re-dumps every session's screen and
# only speaks when something actually changed. One held-open ssh channel beats
# polling: a poll pays the round trip on every single check,
# while this pays it once and then streams.
WATCH_SCRIPT = r"""
last=""
while :; do
  out=$(tmux list-sessions -F "#{session_name}" 2>/dev/null | while read -r s; do
          echo "@@@SESSION:$s"
          tmux capture-pane -p -t "$s" 2>/dev/null | tail -__CAPLINES__
          echo "@@@PANES:$s"
          tmux list-panes -t "$s" -F "#{pane_current_command}" 2>/dev/null
        done)
  now=$(printf '%s' "$out" | cksum)
  if [ "$now" != "$last" ]; then
    last=$now
    printf '%s\n###FRAME\n' "$out"
  fi
  sleep __INTERVAL__
done
"""


def watch_screens(host: str, interval: float = 1.0) -> subprocess.Popen:
    """Start a streaming watcher for one host. Caller owns the process.

    stdin MUST be detached. ssh reads stdin by default, so an ssh started from
    a TUI inherits the terminal and competes with it for keystrokes -- keys
    then land in ssh instead of the app, seemingly at random. That is what -n
    and DEVNULL are for, and it is why this is not optional.
    """
    script = (WATCH_SCRIPT.replace("__INTERVAL__", str(interval))
                          .replace("__CAPLINES__", str(CAPTURE_LINES)))
    return subprocess.Popen(
        ["ssh", "-n", "-o", "BatchMode=yes",
         "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
         "-o", "ServerAliveInterval=15", host, script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)


def parse_frame(lines: list[str]) -> dict[str, dict]:
    """One frame -> {session: {"screen": str, "commands": [str]}}."""
    sessions: dict[str, dict] = {}
    current = None
    mode = None
    for line in lines:
        if line.startswith("@@@SESSION:"):
            current = line[len("@@@SESSION:"):].strip()
            sessions.setdefault(current, {"screen": [], "commands": []})
            mode = "screen"
        elif line.startswith("@@@PANES:"):
            current = line[len("@@@PANES:"):].strip()
            sessions.setdefault(current, {"screen": [], "commands": []})
            mode = "commands"
        elif current and mode == "screen":
            sessions[current]["screen"].append(line.rstrip())
        elif current and mode == "commands" and line.strip():
            sessions[current]["commands"].append(line.strip())

    return {name: {"screen": "\n".join(data["screen"]).strip("\n"),
                   "commands": data["commands"]}
            for name, data in sessions.items()}


def send_text(host: str, session: str, text: str, submit: bool = True) -> None:
    """Type text into a remote session without attaching to it.

    The text goes over as a tmux buffer rather than being interpolated into a
    command line. That means quotes, backticks, $, newlines and anything else a
    prompt might contain arrive verbatim instead of being chewed by two layers
    of shell -- and nothing the user types can turn into a remote command.

    Enter is sent as a separate keystroke: pasting it as part of the buffer
    would land as a literal newline inside the agent's input box instead of
    submitting.
    """
    target = shlex.quote(session)
    steps = []
    if text:
        steps.append("tmux load-buffer -b panel_reply -")
        steps.append(f"tmux paste-buffer -b panel_reply -t {target} -d")
    if submit:
        steps.append(f"tmux send-keys -t {target} Enter")
    if not steps:
        return

    done = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
         host, " && ".join(steps)],
        input=text.encode(), capture_output=True, timeout=25)

    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message else "send failed")


def send_key(host: str, session: str, key: str) -> None:
    """Send a single named key (Escape, C-c, Up...) to a remote session."""
    done = subprocess.run(
        ["ssh", "-n", "-o", "BatchMode=yes",
         "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
         host, f"tmux send-keys -t {shlex.quote(session)} {shlex.quote(key)}"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message else "send failed")


def copy_key(host: str) -> None:
    """ssh-copy-id needs a password typed, so it gets a real terminal window
    rather than being run headless behind the panel."""
    launch(["bash", "-lc",
            f"ssh-copy-id {host}; echo; read -rsn1 -p 'Press any key to close...'"])
