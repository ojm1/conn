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
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import agent_state

# CONN_SSH_CONFIG points everything here that reads or edits the config at
# another file. It exists for the tests: pointed at a config of their own they
# can run without probing the real fleet or rewriting the real file. ssh
# itself never sees the variable -- only this module's parsing and editing.
SSH_CONFIG = Path(os.environ.get("CONN_SSH_CONFIG")
                  or Path.home() / ".ssh" / "config")
# Mountpoints live in the runtime directory, not the home folder. They hold
# nothing -- a mountpoint is an empty hook to hang a filesystem on -- so a
# folder in ~ for each server you once looked at was pure clutter. The runtime
# directory is tmpfs and is cleared at logout, which also settles the leftovers
# for good: a reboot with a mount still up can no longer strand a directory,
# because the directory does not survive either.
MNT_ROOT = Path(os.environ.get("XDG_RUNTIME_DIR")
                or Path.home() / ".cache") / "conn" / "mnt"

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


def run_argv(host: str, opts: list[str] | None = None) -> list[str]:
    """argv that runs, on `host`, the script the caller then feeds to stdin.

    Every probe, watch and send below is POSIX shell that never mentions how it
    got there, so this is the only place that knows the difference: locally the
    script goes to bash directly, remotely to `sh` over ssh. One copy of each
    script, two transports.

    The script travels on stdin rather than as ssh's command argument. The
    argument is handed to the remote *login* shell, and a fish or tcsh user's
    shell rejects POSIX syntax at parse time -- the host then read as down
    with a syntax error for a tooltip. "exec sh" is the one command line every
    shell reads the same way. Feeding stdin ourselves also keeps ssh off the
    terminal: left attached it reads the panel's keystrokes, which is what -n
    used to be here for. A script run this way must not read its own stdin,
    or it eats the rest of its text.

    "--" so a host name is never read as an option, whatever it starts with.
    """
    if is_local(host):
        return ["bash"]

    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    argv += list(opts or [])
    return argv + ["--", host, "exec sh"]


# ---------------------------------------------------------------------------
# ~/.ssh/config
# ---------------------------------------------------------------------------

def _usable_alias(name: str) -> bool:
    """Whether a config alias is one the panel can act on.

    Wildcards are patterns, not hosts. A slash is dropped too -- MNT_ROOT /
    host would follow it out of the mountpoint tree -- and so is a leading
    dash, which reads as an option to ssh, sshfs and everything else an
    alias is handed to.

    bin/ssh-connect carries the attach side's guard: everything this lists
    it must accept, so its rejects are these plus only what breaks its own
    sinks. Change either and check the other.
    """
    return not re.search(r"[*?!/]", name) and not name.startswith("-")


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
                if _usable_alias(name) and name not in names:
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


# The one reference for what may be written into a Host block. These values
# become lines in a file whose directives run commands (ProxyCommand), and the
# alias is later a shell argument and a path segment -- so what cannot be
# written safely is refused, not quoted. A pasted "hostname" with a newline in
# it was one ProxyCommand away from local code execution. add_host and
# update_host both go through here, so the two agree on what is safe.

def _check_alias(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*", name):
        raise HostError("Name must be one word of letters, digits, . _ or -, "
                        "not starting with a dash.")


def _check_hostname(hostname: str) -> None:
    if not hostname:
        raise HostError("Hostname or IP is required.")
    if re.search(r"[^A-Za-z0-9_.:%-]", hostname) or hostname.startswith("-"):
        raise HostError("Hostname can only use letters, digits and . : % _ -, "
                        "and cannot start with a dash.")


def _check_user(user: str) -> None:
    if user and (re.search(r"[^A-Za-z0-9_.@-]", user)
                 or user.startswith("-")):
        raise HostError("User can only use letters, digits and . @ _ -, "
                        "and cannot start with a dash.")


def _check_port(port: str) -> None:
    # Empty is allowed: it means "no Port directive", the default of 22.
    if port and not port.isdigit():
        raise HostError("Port must be a number.")


def _check_identityfile(path: str) -> None:
    # Only the injection is refused -- a newline or other control character
    # would let the value forge a second directive line. Everything else is a
    # path we do not second-guess. Empty means "no IdentityFile directive".
    if re.search(r"[\x00-\x1f\x7f]", path):
        raise HostError("Identity file path cannot contain control characters.")


def add_host(name: str, hostname: str, user: str, port: str = "22") -> Path:
    """Insert a new Host block above the Host * defaults.

    Order matters to ssh: it is first-match-wins, so a block placed after
    Host * would have the defaults applied before its own settings.
    """
    name = name.strip()
    hostname = hostname.strip()
    user = user.strip()
    port = (port or "22").strip()

    _check_alias(name)
    _check_hostname(hostname)
    _check_user(user)
    if name in list_hosts():
        raise HostError(f"'{name}' is already in ~/.ssh/config.")
    _check_port(port)

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


def config_hosts() -> list[str]:
    """Only the aliases the file actually names.

    list_hosts() invents "local" when the config has no such entry, which is
    right for the panel and wrong for anything that edits the file: you cannot
    remove a line that was never written.
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
                if _usable_alias(name) and name not in names:
                    names.append(name)
    return names


def remove_host(name: str) -> Path:
    """Take a Host block back out of ~/.ssh/config.

    The counterpart add_host() never had, so every server the panel ever added
    was permanent as far as it was concerned.

    A Host line may name several aliases. Dropping one of those is an edit to
    that line, not the removal of a block the other names still depend on --
    deleting it wholesale would quietly take unrelated servers with it.
    """
    name = name.strip()
    if not name or re.search(r"[*?!\s]", name):
        raise HostError("Name must be a single word with no * ? or spaces.")

    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError as exc:
        raise HostError(f"Cannot read ~/.ssh/config: {exc}") from exc

    def aliases(line: str) -> list[str] | None:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            return parts[1:]
        return None

    start = None
    for index, line in enumerate(lines):
        named = aliases(line)
        if named and name in named:
            start = index
            break
    if start is None:
        raise HostError(f"'{name}' is not in ~/.ssh/config.")

    backup = backup_config()
    shared = [n for n in aliases(lines[start]) if n != name]

    if shared:
        lines[start] = "Host " + " ".join(shared)
    else:
        end = start + 1
        while end < len(lines) and aliases(lines[end]) is None:
            end += 1
        # Blank lines and comments sitting directly above the next block
        # introduce it, not us -- add_host() walks over the same banner going
        # the other way. Leave them with the block they belong to.
        while end > start + 1:
            above = lines[end - 1].strip()
            if above and not above.startswith("#"):
                break
            end -= 1
        # The blank line above ours is the separator add_host() wrote.
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start:end]

    SSH_CONFIG.write_text("\n".join(lines).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


# Which ssh directive a Site Manager field is written as, and the order new
# ones are inserted in. The keys are what the GUI and host_config() speak; the
# values are how ssh spells them (case is cosmetic -- ssh reads them either
# way, but a file a person also edits should look like add_host wrote it).
_DIRECTIVE = {"hostname": "HostName", "user": "User",
              "port": "Port", "identityfile": "IdentityFile"}


def _host_aliases(line: str) -> list[str] | None:
    """The aliases a 'Host' line names, or None if the line is not one."""
    parts = line.strip().split()
    if len(parts) >= 2 and parts[0].lower() == "host":
        return parts[1:]
    return None


def _starts_block(line: str) -> bool:
    """Whether a line opens a new scope -- the boundary a Host block ends at.

    ssh applies a block's directives until the next Host *or Match* line, so a
    block that runs up to the next Host alone would reach into a Match block
    sitting between the two: an edit meant for one host could then rewrite or
    delete a directive that belongs to the Match.
    """
    first = line.strip().split()[:1]
    return bool(first) and first[0].lower() in ("host", "match")


def _find_block(lines: list[str], name: str) -> tuple[int, int] | None:
    """(start, end) of the block `name` opens: its Host line, to the line that
    begins the next block. None if no Host line names `name`."""
    start = None
    for index, line in enumerate(lines):
        named = _host_aliases(line)
        if named and name in named:
            start = index
            break
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not _starts_block(lines[end]):
        end += 1
    return start, end


def host_config(name: str) -> dict:
    """The block's OWN hostname/user/port/identityfile, exactly as written.

    For editing the file, not connecting: Host * defaults and Match blocks are
    deliberately left out, so what comes back is only what this block sets and
    an empty string where it sets nothing. resolve() is the other question --
    what ssh will actually use -- and folds those in.
    """
    name = name.strip()
    if name not in config_hosts():
        raise HostError(f"'{name}' is not in ~/.ssh/config.")
    lines = SSH_CONFIG.read_text().splitlines()
    start, end = _find_block(lines, name)
    fields = {"hostname": "", "user": "", "port": "", "identityfile": ""}
    for line in lines[start + 1:end]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            key = parts[0].lower()
            # First wins, the way ssh reads a repeated directive within a block.
            if key in fields and not fields[key]:
                fields[key] = parts[1].strip()
    return fields


def host_line_aliases(name: str) -> list[str]:
    """The other aliases sharing `name`'s Host line, [] if it stands alone.

    A block is edited by the Host line it opens, and that line may name several
    hosts -- the GUI shows these so an edit or a probe reads as applying to all
    of them, which it does.
    """
    name = name.strip()
    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        named = _host_aliases(line)
        if named and name in named:
            return [alias for alias in named if alias != name]
    raise HostError(f"'{name}' is not in ~/.ssh/config.")


def update_host(name: str, fields: dict) -> Path:
    """Set, replace or remove directives in `name`'s block, in place.

    `fields` may carry any of hostname/user/port/identityfile. A validated
    non-empty value sets or replaces that directive; an empty one removes it if
    present. Port "22" removes it too -- 22 is ssh's default, which add_host
    also leaves unwritten. Every other line in the block -- comments,
    ProxyCommand, directives we do not touch -- and every other block is kept
    exactly. Values are checked before the file is opened, so a bad one refuses
    rather than being quoted into a line that could run a command.

    The block edited is the one this alias's Host line opens, even when that
    line is shared; that is correct ssh semantics, and host_line_aliases() is
    how the GUI surfaces the sharing.
    """
    name = name.strip()
    if name not in config_hosts():
        raise HostError(f"'{name}' is not in ~/.ssh/config.")

    changes: dict[str, str] = {}
    for key in ("hostname", "user", "port", "identityfile"):
        if key not in fields:
            continue
        value = (fields[key] or "").strip()
        if key == "hostname" and value:
            _check_hostname(value)
        elif key == "user" and value:
            _check_user(value)
        elif key == "port":
            _check_port(value)
            if value == "22":
                value = ""      # the default -- carried by absence, not a line
        elif key == "identityfile" and value:
            _check_identityfile(value)
        changes[key] = value

    backup = backup_config()
    lines = SSH_CONFIG.read_text().splitlines()
    start, end = _find_block(lines, name)
    body = lines[start + 1:end]

    inserts: list[str] = []
    for key, value in changes.items():
        # Every occurrence, not the first: ssh is first-match-wins, so leaving
        # a stale second copy of a directive behind would quietly override the
        # edit -- clearing HostName but leaving a duplicate line is a rename
        # that did not take. A block may legitimately repeat IdentityFile too.
        matches = [i for i, line in enumerate(body)
                   if line is not None
                   and (line.strip().split(None, 1)[:1] or [""])[0].lower() == key]
        new_line = f"    {_DIRECTIVE[key]} {value}"
        if value and not matches:
            inserts.append(new_line)
        elif value:
            body[matches[0]] = new_line          # the first carries the value
            for extra in matches[1:]:
                body[extra] = None               # the rest are stale copies
        else:
            for index in matches:                # remove: every occurrence goes
                body[index] = None
    body = [line for line in body if line is not None]

    # New directives go directly under the Host line, in the order above, so
    # the result reads the way add_host lays a fresh block out.
    merged = lines[:start + 1] + inserts + body + lines[end:]
    SSH_CONFIG.write_text("\n".join(merged).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


def rename_host(old: str, new: str) -> Path:
    """Swap the `old` alias token for `new` on its Host line, nothing else.

    Any other alias on the line stays, and the block's directives are
    untouched -- this is a relabel, not a move. The config is only half the
    rename: the keyring secrets, stars and hand-arranged order are filed under
    the old name and are the caller's to migrate (migrate_secrets does the
    keyring half).
    """
    old = old.strip()
    new = new.strip()
    _check_alias(new)
    if new in list_hosts():
        raise HostError(f"'{new}' is already in ~/.ssh/config.")

    try:
        lines = SSH_CONFIG.read_text().splitlines()
    except OSError as exc:
        raise HostError(f"Cannot read ~/.ssh/config: {exc}") from exc

    target_line = None
    for index, line in enumerate(lines):
        named = _host_aliases(line)
        if named and old in named:
            target_line = index
            break
    if target_line is None:
        raise HostError(f"'{old}' is not in ~/.ssh/config.")

    backup = backup_config()
    swapped = [new if alias == old else alias
               for alias in _host_aliases(lines[target_line])]
    lines[target_line] = "Host " + " ".join(swapped)
    SSH_CONFIG.write_text("\n".join(lines).rstrip("\n") + "\n")
    SSH_CONFIG.chmod(0o600)
    return backup


# ---------------------------------------------------------------------------
# Probing a live host
# ---------------------------------------------------------------------------

# One round trip collects everything the panel shows. Each section is opened
# by a marker line so the parse below cannot be confused by a value that
# happens to contain a newline -- and every marker carries __BOUND__, a random
# token minted fresh for each run and never shown to the far side's screens.
# The capture sections replay whatever a watched terminal chooses to display,
# and a screen that merely *prints* marker text -- an editor open on this
# file, an agent fed hostile output -- must not be able to end a section,
# speak as another session, or forge one that does not exist.
#
# The pane list rides inside the capture loop, under the same guarded
# markers, so the session name never shares a '|'-delimited line with fields
# that are free to contain '|' themselves.
REMOTE_PROBE = r"""
echo "###__BOUND__:UPTIME"; uptime -p 2>/dev/null || uptime 2>/dev/null
echo "###__BOUND__:LOAD";   cut -d' ' -f1-3 /proc/loadavg 2>/dev/null
echo "###__BOUND__:CPUS";   nproc 2>/dev/null
echo "###__BOUND__:MEM";    free -m 2>/dev/null | awk '/^Mem:/{print $3" "$2}'
echo "###__BOUND__:DISK";   df -h / 2>/dev/null | tail -1 | awk '{print $3" "$2" "$5}'
echo "###__BOUND__:TMUX";   tmux list-sessions -F "#{session_name}|#{session_windows}|#{?session_attached,attached,detached}|#{session_activity}" 2>/dev/null
echo "###__BOUND__:CAPTURE"
tmux list-sessions -F "#{session_name}" 2>/dev/null | while read -r s; do
  printf '%s\n' "@@@__BOUND__:SESSION:$s"
  tmux capture-pane -p -e -t "$s" 2>/dev/null | tail -__CAPLINES__
  printf '%s\n' "@@@__BOUND__:PANES:$s"
  tmux list-panes -t "$s" -F "#{pane_current_command}|#{pane_current_path}" 2>/dev/null
done
echo "###__BOUND__:END"
"""

SECTIONS = ("UPTIME", "LOAD", "CPUS", "MEM", "DISK", "TMUX", "CAPTURE", "END")


def _split_captures(text: str, token: str) -> tuple[dict[str, str],
                                                    dict[str, list[dict]]]:
    """Pull the per-session screen grabs and pane lists out of the raw probe
    output, keyed by session name.

    Unlike every other section the screens must survive verbatim: blank lines
    and leading spaces are part of what makes a terminal screen readable, and
    the state classifier reads them. capture-pane runs with -e so they keep
    their colour too: dim is the only thing telling a suggestion sitting in
    the input box apart from something you typed and left there.

    A pane line is command then path. The command goes first because it is
    the one the classifier depends on, and a path is the field with '|' in
    the wild -- partition keeps whatever follows the first '|' as the path,
    '|'s and all.
    """
    start = text.find(f"###{token}:CAPTURE")
    if start < 0:
        return {}, {}
    body = text[start:]
    end = body.find(f"###{token}:END")
    if end >= 0:
        body = body[:end]

    session_mark = f"@@@{token}:SESSION:"
    panes_mark = f"@@@{token}:PANES:"
    screens: dict[str, list[str]] = {}
    panes: dict[str, list[dict]] = {}
    current = None
    mode = None
    for line in body.splitlines():
        if line.startswith(session_mark):
            current = line[len(session_mark):].strip()
            screens.setdefault(current, [])
            mode = "screen"
        elif line.startswith(panes_mark):
            current = line[len(panes_mark):].strip()
            panes.setdefault(current, [])
            mode = "panes"
        elif current is not None and mode == "screen":
            screens[current].append(line.rstrip())
        elif current is not None and mode == "panes" and line.strip():
            cmd, _, path = line.partition("|")
            panes[current].append({"cmd": cmd, "path": path})
    return ({name: "\n".join(lines).strip("\n")
             for name, lines in screens.items()}, panes)


def _split_sections(text: str, token: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {name: [] for name in SECTIONS}
    prefix = f"###{token}:"
    current = None
    for line in text.split(f"{prefix}CAPTURE")[0].splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix) and stripped[len(prefix):] in found:
            current = stripped[len(prefix):]
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


def _probe_failure(last: str, host: str) -> tuple[str, str]:
    """The state and reason for a probe that reached ssh but never opened the
    session -- three problems with three different fixes.

    A key ssh will not trust yet is first contact, not a dead box: a server you
    just added answers, but the probe runs BatchMode and will not accept the
    key for you, so it gets its own state and says how to log in -- rather than
    sitting there red as "down", which reads as broken. Being let to the door
    and refused (Permission denied) is the "no key" state: the box is up, the
    login is not. Everything else is genuinely down. There is no authentication
    on this machine, so a local failure is never a key problem.
    """
    if "Host key verification failed" in last:
        return ("unverified",
                "new server -- ssh does not trust its key yet. Right-click it "
                "and open a session once to answer the fingerprint prompt and "
                "log in; every probe after that rides the key you accept.")
    if "Permission denied" in last and not is_local(host):
        return ("nokey", last)
    return ("down", last)


def probe(host: str) -> dict:
    """Query one host. Never raises -- failure is a state, not an exception,
    because a dashboard that crashes on an offline box is useless."""
    row = blank(host)
    row["target"] = target(resolve(host))
    row["mounted"] = is_mounted(host)
    row["checked"] = time.time()

    token = secrets.token_hex(16)
    script = (REMOTE_PROBE.replace("__CAPLINES__", str(CAPTURE_LINES))
                          .replace("__BOUND__", token))
    # StrictHostKeyChecking=yes, never accept-new: this path runs unattended
    # for every configured host, and silently pinning whatever key the
    # network offers is the one trust decision ssh exists to put in front of
    # a human. An unknown key is a down-state with the fix in the tooltip;
    # the fingerprint prompt itself belongs to the first interactive connect.
    try:
        done = subprocess.run(
            run_argv(host, opts=["-o", "StrictHostKeyChecking=yes"]),
            input=script,
            capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        row["state"] = "down"
        row["error"] = "timed out"
        return row
    except OSError as exc:
        row["state"] = "down"
        row["error"] = str(exc)
        return row

    if f"###{token}:END" not in done.stdout:
        stderr = (done.stderr or "").strip().splitlines()
        last = stderr[-1] if stderr else "unreachable"
        row["state"], row["error"] = _probe_failure(last, host)
        return row

    parts = _split_sections(done.stdout, token)
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

    screens, panes = _split_captures(done.stdout, token)
    mine = own_session() if is_local(host) else ""
    for line in parts["TMUX"]:
        # From the right: the name is the one field free to contain '|', and
        # it comes first, so the three fixed fields are peeled off the end.
        bits = line.rsplit("|", 3)
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

# Where the kernel's table is read from. A name so the tests can hand these
# checks a table of their own.
MOUNTS_TABLE = "/proc/self/mounts"


def _in_mounts_table(path: Path) -> bool:
    """Whether the kernel lists `path` as a mountpoint.

    Deliberately not os.path.ismount(): that stats the path, and a stat on a
    FUSE mount whose sshfs has hung blocks indefinitely -- on the main thread
    that froze the whole window. It also answers wrongly for an sshfs that
    died: the stat fails with ENOTCONN and ismount says False, yet the mount
    is still held and unmounting is the one thing left to do with it. The
    table has neither problem: reading it never enters the filesystem, and a
    dead mount stays listed until it is released.
    """
    # The table octal-escapes space, tab, newline and backslash, so the
    # comparison is made in its terms.
    wanted = (str(path).replace("\\", "\\134").replace(" ", "\\040")
              .replace("\t", "\\011").replace("\n", "\\012"))
    try:
        with open(MOUNTS_TABLE) as table:
            for line in table:
                fields = line.split()
                if len(fields) > 1 and fields[1] == wanted:
                    return True
    except OSError:
        pass
    return False


def is_mounted(host: str) -> bool:
    # This machine's files are already here, which is the state "mounted"
    # exists to describe, so f opens them and u has nothing to undo.
    if is_local(host):
        return True
    return _in_mounts_table(MNT_ROOT / host)


def files_root(host: str) -> Path:
    """Where this host's files are on disk, once they are reachable."""
    return Path.home() if is_local(host) else MNT_ROOT / host


def files_label(host: str) -> str:
    """That path as a prompt would write it."""
    return "~" if is_local(host) else "files"


def mount(host: str) -> str:
    if is_local(host):
        return str(files_root(host))
    # TimeoutExpired is a SubprocessError, not an OSError: left unconverted
    # it escapes the (HostError, OSError) net every caller holds and kills
    # the worker thread, and the click looks like it did nothing.
    try:
        done = subprocess.run(["ssh-mount", host], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        raise HostError("no answer from sshfs after 40s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()
        raise HostError(message.splitlines()[-1] if message else "sshfs failed")
    return str(MNT_ROOT / host)


def unmount(host: str) -> None:
    if is_local(host):
        raise HostError("local is this machine -- nothing to unmount.")
    try:
        done = subprocess.run(["ssh-mount", "-u", host], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer from fusermount after 20s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip()
        raise HostError(message.splitlines()[-1] if message else "unmount failed")


def tidy_mounts() -> list[str]:
    """Take away mountpoints with nothing mounted on them.

    ~/mnt/<host> is created before sshfs runs and outlives it: a mount that
    fails leaves the empty directory behind, and so does a reboot with one
    still up, since no unmount ever runs to tidy it. What is left looks
    exactly like a mount that worked until you open it.

    rmdir only ever removes an empty directory, so this cannot take anything
    with it. If files were written into a mountpoint while it was unmounted --
    the one case where this would be destructive -- the directory is not empty
    and is left exactly where it is.
    """
    gone: list[str] = []
    try:
        entries = sorted(MNT_ROOT.iterdir())
    except OSError:
        return gone
    for path in entries:
        # Table first: anything mounted -- alive, dead or hung -- is skipped
        # before a stat can touch it, so what remains stats plain tmpfs.
        if _in_mounts_table(path):
            continue
        try:
            if not path.is_dir():
                continue
            path.rmdir()
            gone.append(path.name)
        except OSError:
            continue        # in use, not empty, or not ours to remove
    return gone


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


SESSION_NAME = re.compile(r"[^A-Za-z0-9_-]")


def session_name(wanted: str) -> str:
    """The name tmux will actually use for a session asked for as `wanted`.

    Session names are interpolated into shell scripts, so every path filters
    them to [A-Za-z0-9_-] rather than quoting: a session name is a label, and
    one that needs quoting is a mistake. The filter lives here, once, so the
    panel can file a view under the name the session really gets -- type
    "web app" and the view, the row and the tmux session all agree on
    "web_app", instead of the view waiting forever on a name that never
    exists.
    """
    return SESSION_NAME.sub("_", wanted.strip()) or "shell"


def connect_argv(host: str, session: str) -> list[str]:
    """How to attach to one session, wherever it lives.

    Both halves are argv for a real terminal -- suspend the panel over it, or
    hand it to launch() for a window of its own.
    """
    name = session_name(session)
    if is_local(host):
        return ["bash", "-c", LOCAL_CONNECT.replace("__SESSION__", name)]
    return ["ssh-connect", host, name]


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
#
# The markers carry the same per-invocation token as the probe's, for the
# same reason: a frame boundary a watched screen can print is a frame
# boundary it can forge. The ALIVE line every tick is the liveness the
# transport cannot give -- ServerAliveInterval is ignored when ssh is a mux
# client riding the user's ControlMaster, so a master whose TCP has silently
# died leaves the reader waiting on a pipe that will never speak again. The
# heartbeat makes silence finite: a reader that has heard nothing for the
# stall budget knows the channel is dead, not idle. Every tick, not every
# few: a tick is the sleep plus the whole dump -- some twenty process spawns
# -- so on a slow remote a multi-tick beat could outrun the reader's
# WATCH_STALL budget and get a healthy stream killed as stalled. One printf
# a second is nothing next to the dump it follows.
WATCH_SCRIPT = r"""
last=""
while :; do
  out=$(tmux list-sessions -F "#{session_name}" 2>/dev/null | while read -r s; do
          printf '%s\n' "@@@__BOUND__:SESSION:$s"
          tmux capture-pane -p -e -t "$s" 2>/dev/null | tail -__CAPLINES__
          printf '%s\n' "@@@__BOUND__:PANES:$s"
          tmux list-panes -t "$s" -F "#{pane_current_command}" 2>/dev/null
        done)
  now=$(printf '%s' "$out" | cksum)
  if [ "$now" != "$last" ]; then
    last=$now
    printf '%s\n###__BOUND__:FRAME\n' "$out"
  fi
  echo "###__BOUND__:ALIVE"
  sleep __INTERVAL__
done
"""


def watch_screens(host: str, interval: float = 1.0) -> subprocess.Popen:
    """Start a streaming watcher for one host. Caller owns the process.

    stdin carries the script and nothing else -- it is a pipe of ours, never
    the terminal, so ssh cannot compete with the panel for keystrokes the way
    an inherited stdin used to let it.

    The process comes back carrying its invocation's marker strings: `frame`
    ends a frame, `alive` is the idle heartbeat, and `boundary` is the token
    parse_frame needs. They live on the handle because they are only good for
    this one stream -- the next connection mints its own.

    stdout is a raw, unbuffered pipe. The reader select()s on the fd to put a
    ceiling on silence, and a buffered text wrapper would hold lines a select
    on the fd can no longer see.
    """
    token = secrets.token_hex(16)
    script = (WATCH_SCRIPT.replace("__INTERVAL__", str(interval))
                          .replace("__CAPLINES__", str(CAPTURE_LINES))
                          .replace("__BOUND__", token))
    proc = subprocess.Popen(
        run_argv(host, opts=["-o", "ServerAliveInterval=15"]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0)
    try:
        proc.stdin.write(script.encode())
        proc.stdin.close()
    except OSError:
        pass        # died at spawn: the reader sees EOF and backs off
    proc.boundary = token
    proc.frame = f"###{token}:FRAME"
    proc.alive = f"###{token}:ALIVE"
    return proc


def parse_frame(lines: list[str], host: str = "",
                token: str = "") -> dict[str, dict]:
    """One frame -> {session: {"screen": str, "commands": [str]}}.

    `token` is the boundary the frame arrived under -- proc.boundary from the
    watch_screens that produced it. A marker without it is screen content.

    Our own session is dropped here too, not just in probe(). The panel treats
    "the stream and the probe disagree about which sessions exist" as a session
    having appeared, so filtering one and not the other would re-probe on every
    single frame.
    """
    session_mark = f"@@@{token}:SESSION:"
    panes_mark = f"@@@{token}:PANES:"
    sessions: dict[str, dict] = {}
    current = None
    mode = None
    for line in lines:
        if line.startswith(session_mark):
            current = line[len(session_mark):].strip()
            sessions.setdefault(current, {"screen": [], "commands": []})
            mode = "screen"
        elif line.startswith(panes_mark):
            current = line[len(panes_mark):].strip()
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
        message = (done.stderr or "").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "no such secret")
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
        message = (done.stderr or "").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "could not store")


def secret_clear(host: str, name: str) -> None:
    try:
        done = subprocess.run(
            ["secret-tool", "clear", "application", SECRET_APP,
             "host", host, "name", name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or "").strip()
        raise HostError(message.splitlines()[-1] if message
                        else "could not remove")


def migrate_secrets(old: str, new: str) -> None:
    """Re-file every keyring secret from host=old to host=new.

    The keyring is a separate store from the config, keyed by the alias:
    rename_host moves the Host line, this moves the secrets that were filed
    against the old name so they still answer once it is renamed. Store before
    clear, so a run interrupted between the two leaves a copy under both names
    rather than none. A keyring with nothing to move is not an error.
    """
    for name in secret_names(old):
        secret_store(new, name, secret_value(old, name))
        secret_clear(old, name)


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
    try:
        done = subprocess.run(
            run_argv(host),
            input=f"tmux kill-session -t {shlex.quote(session)}".encode(),
            capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer after 20s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
    if done.returncode != 0:
        message = (done.stderr or b"").decode(errors="replace").strip()
        raise HostError(message.splitlines()[-1] if message else "kill failed")


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
    try:
        done = subprocess.run(run_argv(host), input=script.encode(),
                              capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise HostError("no answer after 20s")
    except subprocess.SubprocessError as exc:
        raise HostError(str(exc))
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
    # The alias travels as an argument, never inside the -c string: it is
    # data from the config, and bash would run whatever it contained.
    launch(["bash", "-lc",
            'ssh-copy-id -- "$1"; echo; '
            "read -rsn1 -p 'Press any key to close...'",
            "ssh-copy-id", host])
