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

# The machine the panel itself runs on, listed as a host under this reserved
# name. It is not "ssh to yourself": run_argv hands the same scripts straight
# to bash, so the local rows cost no daemon, no key and no round trip.
LOCAL = "local"

CONNECT_TIMEOUT = 6      # seconds to get a TCP + auth handshake
CAPTURE_LINES = 200     # lines of each remote screen to bring back;
                        # the panel renders as many as it can fit
PROBE_TIMEOUT = 15       # hard ceiling on the whole probe


class HostError(RuntimeError):
    """Something the user needs to read, not a traceback."""


# ---------------------------------------------------------------------------
# Transport: where a script runs
# ---------------------------------------------------------------------------

def is_local(host: str) -> bool:
    return host == LOCAL


_OWN_SESSION: str | None = None


def own_session() -> str:
    """The local tmux session the panel is running inside, or "".

    Normally "" -- conn gets a window of its own. Started from inside tmux it
    is a real session, and listing it would offer to attach to the session you
    are already in: the one place an attach cannot go. So it is left out.
    """
    global _OWN_SESSION
    if _OWN_SESSION is None:
        _OWN_SESSION = ""
        if os.environ.get("TMUX"):
            try:
                out = subprocess.run(["tmux", "display-message", "-p", "#S"],
                                     stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=5)
                _OWN_SESSION = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    return _OWN_SESSION


def run_argv(host: str, script: str, opts: list[str] | None = None,
             stdin: bool = False) -> list[str]:
    """argv that runs `script` on `host`.

    Every probe, watch and send below is POSIX shell that never mentions how it
    got there, so this is the only place that knows the difference: locally the
    script goes to bash directly, remotely it goes to ssh. One copy of each
    script, two transports.

    `stdin` keeps the channel open for callers that feed the script input; ssh
    otherwise reads stdin by default and steals the panel's keystrokes, which
    is why -n is on everywhere else.
    """
    if is_local(host):
        return ["bash", "-c", script]

    argv = ["ssh"]
    if not stdin:
        argv.append("-n")
    argv += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    argv += list(opts or [])
    return argv + [host, script]


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
        text = ""

    names: list[str] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            for name in parts[1:]:
                if not re.search(r"[*?!]", name) and name not in names:
                    names.append(name)

    # This machine leads the list: it is the one host that is always up, and
    # the sessions on it are the ones you are most likely to be mid-thought in.
    # A config that already defines "local" keeps it -- an alias the user can
    # actually ssh to beats one the panel invented. CONN_NO_LOCAL=1 hides it.
    if LOCAL not in names and os.environ.get("CONN_NO_LOCAL") != "1":
        names.insert(0, LOCAL)
    return names


def resolve(host: str) -> dict:
    """Ask ssh itself what a name expands to, rather than re-parsing config.

    This picks up Match blocks, Include files and the Host * defaults, so the
    panel shows the connection that will actually be made.
    """
    info = {"user": "", "hostname": "", "port": "", "identityfile": ""}
    if is_local(host):
        # Nothing to resolve, and no connection to describe. Saying so beats
        # printing a loopback address that no ssh will ever be made to.
        return info | {"hostname": "this machine", "port": "22"}
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
  tmux capture-pane -p -e -t "$s" 2>/dev/null | tail -__CAPLINES__
done
echo "###END"
"""

SECTIONS = ("UPTIME", "LOAD", "CPUS", "MEM", "DISK", "TMUX", "PANES", "CAPTURE", "END")


def _split_captures(text: str) -> dict[str, str]:
    """Pull the per-session screen grabs out of the raw probe output.

    Unlike every other section these must survive verbatim: blank lines and
    leading spaces are part of what makes a terminal screen readable, and the
    state classifier reads them. capture-pane runs with -e so they keep their
    colour too: dim is the only thing telling a suggestion sitting in the
    input box apart from something you typed and left there.
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
            run_argv(host,
                     REMOTE_PROBE.replace("__CAPLINES__", str(CAPTURE_LINES)),
                     opts=["-o", "StrictHostKeyChecking=accept-new"]),
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
        # There is no authentication on this machine, so a local failure is
        # never that -- offering to install a key would be nonsense.
        refused = "Permission denied" in last and not is_local(host)
        row["state"] = "nokey" if refused else "down"
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
    mine = own_session() if is_local(host) else ""
    for line in parts["TMUX"]:
        bits = line.split("|")
        if len(bits) < 3:
            continue
        name = bits[0]
        if name == mine:
            continue
        raw = screens.get(name, "")
        screen = agent_state.strip_ansi(raw)
        commands = [pane["cmd"] for pane in panes.get(name, [])]
        row["sessions"].append({
            "name": name, "windows": bits[1],
            "attached": bits[2] == "attached",
            "activity": int(bits[3]) if len(bits) > 3 and bits[3].isdigit() else 0,
            "panes": panes.get(name, []),
            "screen": screen,
            "agent": agent_state.classify(screen, commands, raw),
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
    # This machine's files are already here, which is the state "mounted"
    # exists to describe, so f opens them and u has nothing to undo.
    if is_local(host):
        return True
    try:
        return os.path.ismount(MNT_ROOT / host)
    except OSError:
        return False


def files_root(host: str) -> Path:
    """Where this host's files are on disk, once they are reachable."""
    return Path.home() if is_local(host) else MNT_ROOT / host


def files_label(host: str) -> str:
    """That path as a prompt would write it."""
    return "~" if is_local(host) else f"~/mnt/{host}"


def mount(host: str) -> str:
    if is_local(host):
        return str(files_root(host))
    done = subprocess.run(["ssh-mount", host], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=40)
    if done.returncode != 0:
        raise HostError((done.stderr or done.stdout or "sshfs failed").strip().splitlines()[-1])
    return str(MNT_ROOT / host)


def unmount(host: str) -> None:
    if is_local(host):
        raise HostError("local is this machine -- nothing to unmount.")
    done = subprocess.run(["ssh-mount", "-u", host], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        raise HostError((done.stderr or done.stdout or "unmount failed").strip().splitlines()[-1])


def open_files(path: str) -> None:
    subprocess.Popen(["xdg-open", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# Attaching on this machine is the session ssh-connect builds on the far side
# with the transport taken out: same name, same F12, same banner, so a local
# chat and a remote one behave identically once you are inside them.
#
# unset TMUX is what makes it work when the panel itself was started from
# inside tmux. tmux refuses a nested attach outright; without this the key
# would look like it did nothing. Unset, it becomes a second client on the
# same server -- which works, at the cost of the inner status line being drawn
# under the outer one.
LOCAL_CONNECT = r"""
printf '\033]0;%s\007' 'local/__SESSION__'
unset TMUX
exec tmux new-session -A -s '__SESSION__' \
  \; bind-key -n F12 detach-client \
  \; set-option -t '__SESSION__' status-left-length 40 \
  \; set-option -t '__SESSION__' status-left '[local/__SESSION__] ' \
  \; set-option -t '__SESSION__' display-time 5000 \
  \; display-message 'F12  or  ctrl-b d   =   back to the server panel'
"""


def connect_argv(host: str, session: str) -> list[str]:
    """How to attach to one session, wherever it lives.

    Both halves are argv for a real terminal -- suspend the panel over it, or
    hand it to launch() for a window of its own.
    """
    if is_local(host):
        # The name is interpolated into a shell script, so it is filtered the
        # same way ssh-connect filters its own argument rather than quoted:
        # a session name is a label, and one that needs quoting is a mistake.
        name = re.sub(r"[^A-Za-z0-9_-]", "_", session) or "shell"
        return ["bash", "-c", LOCAL_CONNECT.replace("__SESSION__", name)]
    return ["ssh-connect", host, session]


SESSION_APP_ID = "org.omarchy.conn-session"
PANEL_APP_ID = "org.omarchy.conn"


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
                                   Path.home() / ".cache")) / "conn" / "last-launch-error"


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
          tmux capture-pane -p -e -t "$s" 2>/dev/null | tail -__CAPLINES__
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
        run_argv(host, script, opts=["-o", "ServerAliveInterval=15"]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)


def parse_frame(lines: list[str], host: str = "") -> dict[str, dict]:
    """One frame -> {session: {"screen": str, "commands": [str]}}.

    Our own session is dropped here too, not just in probe(). The panel treats
    "the stream and the probe disagree about which sessions exist" as a session
    having appeared, so filtering one and not the other would re-probe on every
    single frame.
    """
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

    # Only ours, and only here: a remote box may well have a session with the
    # same name, and it is a different session on a different machine.
    mine = own_session() if is_local(host) else ""
    frames = {}
    for name, data in sessions.items():
        if name == mine:
            continue
        raw = "\n".join(data["screen"]).strip("\n")
        frames[name] = {"screen": agent_state.strip_ansi(raw), "raw": raw,
                        "commands": data["commands"]}
    return frames


# ---------------------------------------------------------------------------
# The agent, and the secrets beside it
# ---------------------------------------------------------------------------

# Where a desktop keeps its ssh agent, in the order worth trying.
AGENT_SOCKETS = ("gcr/ssh",            # gnome-keyring, via gcr
                 "ssh-agent.socket",   # systemd's own user agent
                 "keyring/ssh")        # older gnome-keyring


def ensure_agent() -> str:
    """Find the agent when nothing in the environment points at one.

    SSH_AUTH_SOCK is usually exported by a shell profile, so a panel started
    from a launcher rather than a terminal has never heard of it. That is not
    obvious from the outside: ssh keeps working for a while on the
    multiplexed connections ControlPersist is holding open, and only once
    those age out does every host start refusing at once.

    Setting it here means every ssh, ssh-add and secret-tool spawned below
    inherits it, whichever way conn was started.
    """
    current = os.environ.get("SSH_AUTH_SOCK", "")
    try:
        if current and Path(current).is_socket():
            return current
    except OSError:
        pass

    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    for name in AGENT_SOCKETS:
        candidate = runtime / name
        try:
            if candidate.is_socket():
                os.environ["SSH_AUTH_SOCK"] = str(candidate)
                return str(candidate)
        except OSError:
            continue
    return ""


def agent_keys() -> int | None:
    """How many identities the ssh agent is holding, or None if there is no
    agent to ask.

    This is the whole of conn's involvement with your passphrase. The key is
    unlocked once -- by the desktop keyring at login, or by the button below
    -- and every ssh spawned from here rides the agent. Holding the passphrase
    ourselves would add a second copy of it and protect nothing.
    """
    try:
        done = subprocess.run(["ssh-add", "-l"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 2:          # could not talk to an agent at all
        return None
    if done.returncode == 1:          # agent is there, holding nothing
        return 0
    return len([line for line in done.stdout.splitlines() if line.strip()])


def unlock_agent() -> None:
    """Ask ssh-add for the passphrase, in a terminal of its own.

    Deliberately not a dialog of ours: the prompt belongs to ssh-add and the
    keyring, so the passphrase goes from your keyboard to them without conn
    ever being on the path.
    """
    launch(["bash", "-lc",
            "ssh-add; echo; read -rsn1 -p 'Press any key to close...'"])


# Secrets live in the desktop keyring, which your login password already
# unlocks -- so "one password for everything" is not a feature to build here,
# it is the arrangement that exists. conn stores nothing itself and reads a
# value only when you ask to see it.
SECRET_APP = "conn"


def secret_names(host: str) -> list[str]:
    """What is filed against this host. Names only -- the values stay put
    until something asks for one by name."""
    try:
        done = subprocess.run(
            ["secret-tool", "search", "--all",
             "application", SECRET_APP, "host", host],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []

    # secret-tool prints the attributes on stderr and everything else on
    # stdout, so both have to be read or the listing comes back empty while
    # every individual lookup works.
    names = []
    for line in (done.stdout + "\n" + done.stderr).splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "attribute.name":
            name = value.strip()
            if name and name not in names:
                names.append(name)
    return sorted(names)


def secret_value(host: str, name: str) -> str:
    try:
        done = subprocess.run(
            ["secret-tool", "lookup", "application", SECRET_APP,
             "host", host, "name", name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        raise HostError((done.stderr or "no such secret").strip().splitlines()[-1])
    return done.stdout


def secret_store(host: str, name: str, value: str) -> None:
    """Hand a value to the keyring. It arrives on stdin rather than in the
    command line, which anything on the machine can read."""
    if not name.strip():
        raise HostError("A name is required.")
    try:
        done = subprocess.run(
            ["secret-tool", "store", "--label", f"conn: {host}/{name}",
             "application", SECRET_APP, "host", host, "name", name],
            input=value, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        raise HostError((done.stderr or "could not store").strip().splitlines()[-1])


def secret_clear(host: str, name: str) -> None:
    try:
        done = subprocess.run(
            ["secret-tool", "clear", "application", SECRET_APP,
             "host", host, "name", name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        raise HostError((done.stderr or "could not remove").strip().splitlines()[-1])


# What a URL looks like, minus the punctuation that ends a sentence rather
# than an address.
URL = re.compile(r"(?:https?://|ftp://|file://|mailto:)"
                 r"[^\s<>\"'`{}|\\^\[\]]*[^\s<>\"'`{}|\\^\[\].,;:!?)]")


def screen_links(screen: str) -> list[str]:
    """Every URL on a screen, including ones wrapped across rows.

    A terminal can normally join a wrapped line back together, because it
    knows it wrapped it. Inside tmux nothing does: tmux redraws a pane row by
    row, and a long URL arrives as several lines with no record that they were
    ever one. That is why clicking a login link works until the link is long
    enough to matter.

    What survives is its shape -- a row ending without a space, followed by a
    row that is one unbroken token starting hard against the left margin --
    and continuation is only considered while the line so far is already a
    URL. Two ordinary full-width lines do not meet that.
    """
    out: list[str] = []
    buffer = ""
    for raw in screen.splitlines():
        line = raw.rstrip()
        token = line.strip()
        if (buffer and "://" in buffer and token and " " not in token
                and not raw[:1].isspace()):
            buffer += token
            continue
        if buffer:
            out.append(buffer)
        buffer = line
    if buffer:
        out.append(buffer)

    found: list[str] = []
    for line in out:
        for match in URL.finditer(line):
            url = match.group(0)
            if url not in found:
                found.append(url)
    return found


def kill_session(host: str, session: str) -> None:
    """End a tmux session and everything running in it.

    There is no undo and no scrollback afterwards: whatever the agent was
    part-way through is gone. Callers ask first.
    """
    done = subprocess.run(
        run_argv(host, f"tmux kill-session -t {shlex.quote(session)}"),
        stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message else "kill failed")


SESSION_NAME = re.compile(r"[^A-Za-z0-9_-]")


def rename_session(host: str, session: str, wanted: str) -> str:
    """Rename a tmux session, here or on a server. Returns the name it got.

    Nothing is interrupted: tmux renames the session out from under whatever
    is running in it, and a terminal already attached stays attached. The
    label in tmux's own status bar is rewritten to match, because it was
    stamped with the old name at attach time and would otherwise go on
    claiming to be a session that no longer exists.

    The name is filtered the way connect_argv filters one, for the same
    reason: it is interpolated into a shell script, and a session name that
    needs quoting is a mistake rather than a thing to support.
    """
    name = SESSION_NAME.sub("_", wanted.strip())
    if not name:
        raise HostError("a session needs a name")
    if name == session:
        return name

    label = f"[{host}/{name}] "
    script = (f"tmux rename-session -t {shlex.quote(session)} {shlex.quote(name)}"
              f" && tmux set-option -t {shlex.quote(name)} status-left "
              f"{shlex.quote(label)}")
    done = subprocess.run(run_argv(host, script), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=20)
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "rename failed")
    return name


def copy_key(host: str) -> None:
    """ssh-copy-id needs a password typed, so it gets a real terminal window
    rather than being run headless behind the panel."""
    if is_local(host):
        raise HostError("local is this machine -- no key needed.")
    launch(["bash", "-lc",
            f"ssh-copy-id {host}; echo; read -rsn1 -p 'Press any key to close...'"])
