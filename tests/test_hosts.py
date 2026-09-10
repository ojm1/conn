#!/usr/bin/env python3
"""The transport, checked without a network.

hosts.py talks to ssh, sshfs, tmux and the keyring. This file checks the
parts that decide things -- frame parsing, marker forgery, the mount table,
error routing -- without touching any of them: every process spawned here is
a fake on PATH or a pipe of our own. No display, no tmux, no remote, and
nothing of yours is read or rewritten.

    python3 tests/test_hosts.py

The notification and stream sections import gui, which needs the GTK stack
installed but no display; on a machine without it they skip rather than fail,
the way test_gui.py does without a display.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hosts  # noqa: E402

# A per-run boundary token, the shape watch_screens mints.
TOKEN = "feedbeeffeedbeef"
RULE = "─" * 100


class Checks:
    def __init__(self):
        self.failures = 0

    def __call__(self, name: str, passed: bool, detail: str = "") -> None:
        self.failures += not passed
        print(f"{'ok  ' if passed else 'FAIL'} {name}"
              + (f"   {detail}" if detail and not passed else ""))


def frame_checks(check: Checks) -> None:
    lines = [
        f"@@@{TOKEN}:SESSION:deploy",
        "\x1b[2msuggestion\x1b[0m line",
        "@@@SESSION:evil",                  # forged: no token at all
        f"@@@{'0' * 16}:SESSION:evil",      # forged: the wrong token
        f"@@@{TOKEN}:PANES:deploy",
        "claude",
        "",
        f"@@@{TOKEN}:SESSION:myself",
        "at a prompt",
        f"@@@{TOKEN}:PANES:myself",
        "bash",
    ]
    # own_session() caches what tmux said at first ask; pinned here so the
    # checks do not depend on whether this test runs inside tmux.
    was, hosts._OWN_SESSION = hosts._OWN_SESSION, "myself"
    try:
        local = hosts.parse_frame(lines, "local", TOKEN)
        remote = hosts.parse_frame(lines, "web-1", TOKEN)
    finally:
        hosts._OWN_SESSION = was

    check("a frame parses into its sessions",
          set(remote) == {"deploy", "myself"}, f"got={sorted(remote)}")
    check("the raw screen keeps its escapes beside the stripped copy",
          "\x1b[2m" in remote["deploy"]["raw"]
          and "\x1b" not in remote["deploy"]["screen"])
    check("a printed marker without the token is content, not a boundary",
          "evil" not in remote
          and "@@@SESSION:evil" in remote["deploy"]["screen"],
          f"sessions={sorted(remote)}")
    check("and so is one carrying somebody else's token",
          "SESSION:evil" in remote["deploy"]["screen"])
    check("the pane commands ride along",
          remote["deploy"]["commands"] == ["claude"],
          f"commands={remote['deploy']['commands']}")
    check("our own session is dropped on the local host",
          set(local) == {"deploy"}, f"got={sorted(local)}")
    check("but a remote session with our name is a different session",
          "myself" in remote)
    check("no lines, no sessions", hosts.parse_frame([], "web-1", TOKEN) == {})


def capture_checks(check: Checks) -> None:
    text = (f"###{TOKEN}:CAPTURE\n"
            f"@@@{TOKEN}:SESSION:deploy\n"
            "  compiling\n"
            "###:END\n"                    # forged: an END with no token
            f"@@@{TOKEN}:PANES:deploy\n"
            "claude|/srv/app|logs\n"       # a path with '|' of its own
            f"###{TOKEN}:END\n")
    screens, panes = hosts._split_captures(text, TOKEN)
    check("the capture ends only at its own END marker",
          "###:END" in screens.get("deploy", ""),
          f"screen={screens.get('deploy')!r}")
    check("a pane path keeps its pipes -- only the first splits",
          panes.get("deploy") == [{"cmd": "claude", "path": "/srv/app|logs"}],
          f"panes={panes.get('deploy')}")
    check("no CAPTURE marker, no captures",
          hosts._split_captures("just output\n", TOKEN) == ({}, {}))

    parts = hosts._split_sections(
        f"###{TOKEN}:UPTIME\nup 3 days\n###:LOAD\n", TOKEN)
    check("a section header without the token is content, not a header",
          parts["UPTIME"] == ["up 3 days", "###:LOAD"] and parts["LOAD"] == [],
          f"parts={parts['UPTIME']}")


def mounts_table_checks(check: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        table = Path(tmp) / "mounts"
        # A root with a space in it, because the kernel octal-escapes the
        # table and the comparison has to be made in its terms.
        root = Path(tmp) / "with space" / "mnt"
        was_root, hosts.MNT_ROOT = hosts.MNT_ROOT, root
        was_table, hosts.MOUNTS_TABLE = hosts.MOUNTS_TABLE, str(table)
        try:
            escaped = str(root / "web-1").replace(" ", "\\040")
            table.write_text(f"web-1: {escaped} fuse.sshfs rw 0 0\n"
                             "tmpfs /tmp tmpfs rw 0 0\n")
            check("a mount in the table is mounted, spaces escaped the "
                  "kernel's way", hosts.is_mounted("web-1"))
            check("one not in it is not", not hosts.is_mounted("web-2"))
            check("local is always mounted -- its files are already here",
                  hosts.is_mounted("local"))
            table.unlink()
            check("an unreadable table reads as not mounted",
                  not hosts.is_mounted("web-1"))
        finally:
            hosts.MNT_ROOT = was_root
            hosts.MOUNTS_TABLE = was_table


def mount_action_checks(check: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "bin" / "ssh-mount"
        fake.parent.mkdir()
        was_path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake.parent}:{was_path}"

        def script(body: str) -> None:
            fake.write_text("#!/bin/bash\n" + body)
            fake.chmod(0o755)

        def raised(call) -> str:
            try:
                call()
            except hosts.HostError as exc:
                return str(exc)
            return ""

        try:
            script("echo mounted\n")
            check("a clean mount comes back as its path",
                  hosts.mount("web-1") == str(hosts.MNT_ROOT / "web-1"))
            script("echo noise >&2\n"
                   "echo 'could not mount web-1: connection refused' >&2\n"
                   "exit 1\n")
            said = raised(lambda: hosts.mount("web-1"))
            check("a refused mount surfaces the last line, readable",
                  said == "could not mount web-1: connection refused",
                  f"said={said!r}")
            script("exit 1\n")
            check("and one that failed silently still says something",
                  raised(lambda: hosts.mount("web-1")) == "sshfs failed")

            script("echo 'could not unmount: busy' >&2\nexit 1\n")
            check("a refused unmount comes back the same way",
                  raised(lambda: hosts.unmount("web-1"))
                  == "could not unmount: busy")
            script("exit 0\n")
            check("a clean unmount raises nothing",
                  hosts.unmount("web-1") is None)

            check("local mounts to the home directory",
                  hosts.mount("local") == str(Path.home()))
            check("and refuses to unmount this machine",
                  "this machine" in raised(lambda: hosts.unmount("local")))
        finally:
            os.environ["PATH"] = was_path


def ssh_mount_script_checks(check: Checks) -> None:
    """bin/ssh-mount itself, against a fake sshfs and mountpoint.

    The guards under test: sshfs exiting non-zero, and sshfs exiting 0 with
    nothing actually mounted. Both must fail out loud and take the just-made
    mountpoint with them, or the leftover directory reads as a mount that
    worked. The listed()-true branches -- already mounted, wedged by a dead
    sshfs -- need a real kernel mount and are left to use.
    """
    script = ROOT / "bin" / "ssh-mount"
    with tempfile.TemporaryDirectory() as tmp:
        fakebin = Path(tmp) / "bin"
        fakebin.mkdir()
        runtime = Path(tmp) / "run"
        runtime.mkdir()
        (fakebin / "sshfs").write_text(
            "#!/bin/bash\n"
            'if [ "$FAKE_SSHFS" = fail ]; then\n'
            '  echo "read: Connection refused" >&2\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n")
        (fakebin / "mountpoint").write_text(
            "#!/bin/bash\n"
            '[ "$FAKE_MOUNTED" = yes ]\n')
        for tool in fakebin.iterdir():
            tool.chmod(0o755)
        env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}",
                   XDG_RUNTIME_DIR=str(runtime))
        mnt = runtime / "conn" / "mnt" / "web-1"

        def run(*args: str, **fakes: str):
            return subprocess.run(["bash", str(script), *args],
                                  env=dict(env, **fakes), capture_output=True,
                                  text=True, timeout=30)

        done = run("web-1", FAKE_SSHFS="fail", FAKE_MOUNTED="no")
        check("a refused sshfs fails out loud",
              done.returncode == 1 and "could not mount web-1" in done.stderr,
              f"rc={done.returncode} err={done.stderr!r}")
        check("and takes its empty mountpoint with it", not mnt.exists())

        done = run("web-1", FAKE_SSHFS="ok", FAKE_MOUNTED="no")
        check("sshfs exiting 0 with nothing mounted is still a failure",
              done.returncode == 1 and "nothing is mounted" in done.stderr,
              f"rc={done.returncode} err={done.stderr!r}")
        check("and leaves no mountpoint either", not mnt.exists())

        done = run("web-1", FAKE_SSHFS="ok", FAKE_MOUNTED="yes")
        check("a mount that verified reports where it is",
              done.returncode == 0 and str(mnt) in done.stdout,
              f"rc={done.returncode} out={done.stdout!r}")
        check("and the mountpoint stands", mnt.exists())

        done = run("-u", "web-1", FAKE_MOUNTED="no")
        check("unmounting what is not mounted says so and succeeds",
              done.returncode == 0 and "not mounted" in done.stdout,
              f"rc={done.returncode} out={done.stdout!r}")
        check("and tidies the leftover directory away", not mnt.exists())


def load_gui():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Vte", "3.91")
        import gui
        return gui
    except (ImportError, ValueError):
        return None


def notification_checks(check: Checks, gui) -> None:
    """The builder half of the notification round trip.

    Gio.Notification has no getters, so the builder runs with its setters
    observed. What matters most is the target it writes: the click only works
    because notification() and open_from_notification() both go through
    session_ref/split_ref, and the target here is checked against that seam.
    The click half -- the target landing back in a window -- needs a display
    and lives in test_gui.py.
    """
    import agent_state
    from gi.repository import Gio

    check("a ref comes back apart, host first",
          gui.split_ref(gui.session_ref("web-1", "deploy"))
          == ("web-1", "deploy"))
    check("however awkward the names",
          gui.split_ref(gui.session_ref("db.stage-2", "run_4"))
          == ("db.stage-2", "run_4"))
    check("what reaches the desktop is printable and bounded",
          gui.notify_text("a\x07b\x9bc" + "d" * 200) == "abc" + "d" * 117)

    built = {}
    watched = ("set_priority", "set_icon", "set_default_action_and_target")
    originals = {name: getattr(Gio.Notification, name) for name in watched}

    def observe(name):
        def observed(self, *args):
            built[name] = args
            return originals[name](self, *args)
        return observed

    for name in watched:
        setattr(Gio.Notification, name, observe(name))
    try:
        waiting = {"name": "deploy",
                   "agent": {"state": agent_state.NEEDS_YOU,
                             "label": "needs you",
                             "detail": "Do you want to?"}}
        gui.notification("web-1", waiting)
        action, target = built["set_default_action_and_target"]
        check("a click is aimed at the open-session action",
              action == "app.open-session", f"action={action}")
        check("and its target is the ref both sides share",
              target.get_string() == gui.session_ref("web-1", "deploy"),
              f"target={target.get_string()!r}")
        check("a session waiting on you interrupts",
              built["set_priority"][0] == Gio.NotificationPriority.HIGH)
        check("under the app's own icon",
              gui.APP_ID in built["set_icon"][0].get_names(),
              f"icon={built['set_icon'][0]}")
        gui.notification("web-1", dict(
            waiting, agent=dict(waiting["agent"],
                                state=agent_state.WORKING,
                                label="working", detail="")))
        check("and anything else does not",
              built["set_priority"][0] == Gio.NotificationPriority.NORMAL)
    finally:
        for name, orig in originals.items():
            setattr(Gio.Notification, name, orig)

    # The receiving action must take exactly the type the builder sends, or
    # the click dies inside GLib with nothing on screen to say so.
    app = gui.ConnApp()
    action = app.lookup_action("open-session")
    check("the app's action takes the type the builder sends",
          action is not None
          and action.get_parameter_type().equal(target.get_type()))


def stream_checks(check: Checks, gui) -> None:
    """The reader that feeds parse_frame, against a pipe of our own.

    ssh delivers the watch stream in arbitrary chunks, so a marker can arrive
    split across two reads; the reader's line buffer is what puts it back
    together. The fake proc here is a real pipe with the payload written in
    deliberately awkward pieces.
    """
    payload = (f"@@@{TOKEN}:SESSION:deploy\n"
               "line one\n"
               "@@@SESSION:forged\n"
               f"@@@{TOKEN}:PANES:deploy\n"
               "claude\n"
               f"###{TOKEN}:FRAME\n"
               f"###{TOKEN}:ALIVE\n").encode()
    read_end, write_end = os.pipe()
    cut = payload.find(b":FRAME")            # split inside a marker line
    for chunk in (payload[:9], payload[9:cut], payload[cut:]):
        os.write(write_end, chunk)

    class Proc:
        def __init__(self):
            self.stdout = os.fdopen(read_end, "rb", buffering=0)
            self.boundary = TOKEN
            self.frame = f"###{TOKEN}:FRAME"
            self.alive = f"###{TOKEN}:ALIVE"

        def terminate(self):
            pass

    class Panel:
        stopping = False

    stub = Panel()
    stub.watchers = {}
    stub.watch_lock = threading.Lock()
    stub.frames = queue.Queue()
    halt, poke = threading.Event(), threading.Event()
    spawned = []

    def fake_watch(host, interval):
        spawned.append(host)
        return Proc()

    was, hosts.watch_screens = hosts.watch_screens, fake_watch
    reader = threading.Thread(target=gui.Conn._stream,
                              args=(stub, "web-1", halt, poke), daemon=True)
    try:
        reader.start()
        try:
            frame = stub.frames.get(timeout=10)
        except queue.Empty:
            frame = None
    finally:
        halt.set()
        os.close(write_end)
        reader.join(timeout=10)
        hosts.watch_screens = was

    check("the reader hands parse_frame a whole frame", frame is not None
          and frame[0] == "web-1" and set(frame[1]) == {"deploy"},
          f"frame={frame}")
    if frame is not None:
        check("with markers split across reads put back together",
              frame[1]["deploy"]["screen"] == "line one\n@@@SESSION:forged"
              and frame[1]["deploy"]["commands"] == ["claude"],
              f"deploy={frame[1]['deploy']}")
    check("a heartbeat is not a frame", stub.frames.empty())
    check("and one connection was enough", spawned == ["web-1"],
          f"spawned={spawned}")
    check("the reader lets go of the stream when told",
          not reader.is_alive() and stub.watchers == {})


def drain_checks(check: Checks, gui) -> None:
    """drain_frames' two rules: same sessions reclassify in place, a changed
    set is a real probe -- the stream only carries screens."""
    import agent_state

    probed: list[str] = []
    landed = threading.Event()

    class Panel:
        stopping = False

        def render(self):
            self.rendered = True

        def _probe(self, host):
            probed.append(host)
            landed.set()

    stub = Panel()
    stub.rendered = False
    stub.frames = queue.Queue()
    stub.inflight = set()
    stub.streamed = {}
    stub.rows = {"web-1": {"sessions": [
        {"name": "deploy", "screen": "",
         "agent": {"state": agent_state.UNKNOWN, "label": "unknown",
                   "detail": ""}}]}}

    idle = (f"✻ Cooked for 12s\n{RULE}\n❯ Try \"fix the bug\"\n{RULE}\n"
            "  ⏵⏵ auto mode on")
    stub.frames.put(("web-1", {"deploy": {"screen": idle, "raw": "",
                                          "commands": ["claude"]}}))
    stub.frames.put(("gone-host", {}))      # a host the panel no longer lists
    gui.Conn.drain_frames(stub)
    session = stub.rows["web-1"]["sessions"][0]
    check("a frame over the same sessions reclassifies in place",
          session["agent"]["state"] == agent_state.READY
          and session["screen"] == idle,
          f"agent={session['agent']}")
    check("renders what changed", stub.rendered
          and stub.streamed.get("web-1", 0) > 0)
    check("and probes nothing", probed == [], f"probed={probed}")

    stub.rendered = False
    stub.frames.put(("web-1", {"deploy": {"screen": idle, "raw": "",
                                          "commands": ["claude"]},
                               "fresh": {"screen": "", "raw": "",
                                         "commands": ["bash"]}}))
    gui.Conn.drain_frames(stub)
    check("a session appearing needs the probe, not the stream",
          landed.wait(5) and probed == ["web-1"]
          and "web-1" in stub.inflight, f"probed={probed}")
    check("so the rows wait for it", not stub.rendered
          and len(stub.rows["web-1"]["sessions"]) == 1)

    probed.clear()
    stub.frames.put(("web-1", {}))          # every session gone, probe busy
    gui.Conn.drain_frames(stub)
    check("but never two probes at once", probed == [], f"probed={probed}")


# ---------------------------------------------------------------------------
# Editing ~/.ssh/config: host_config, host_line_aliases, update_host,
# rename_host. Every edit is made to a config of our own -- hosts.SSH_CONFIG is
# pointed at a temp file and put back afterwards -- so nothing of yours is read
# or rewritten, and the suite never runs ssh itself.
# ---------------------------------------------------------------------------

# A config that carries every shape the editors have to survive: a plain block
# with all four directives, a block sharing its Host line with a second alias,
# a comment banner, a ProxyCommand, an Include and a Match block that must come
# through untouched, and the Host * defaults that must not fold into any block.
RICH = """\
# my servers

Host web-1
    HostName 10.0.0.1
    User deploy
    Port 2222
    # note about this host
    ProxyCommand corkscrew proxy 8080 %h %p

Host db shared-db
    HostName 10.0.0.2

Include ~/.ssh/extra

Match host bastion
    ForwardAgent yes

Host *
    ServerAliveInterval 30
    IdentityFile ~/.ssh/id_ed25519
"""

# The fake keyring: a secret-tool on PATH backed by a JSON file, so
# migrate_secrets is exercised end to end without ever touching the real
# keyring. store/lookup/clear/search are the four verbs hosts.py speaks.
FAKE_SECRET_TOOL = r'''#!/usr/bin/env python3
import json, os, sys
manifest = os.path.join(os.environ["FAKE_KEYRING"], "manifest.json")
args = sys.argv[1:]
cmd = args[0] if args else ""

def val(key):
    for i, a in enumerate(args):
        if a == key and i + 1 < len(args):
            return args[i + 1]
    return None

try:
    with open(manifest) as f:
        items = json.load(f)
except (OSError, ValueError):
    items = []

def save(rows):
    with open(manifest, "w") as f:
        json.dump(rows, f)

host, name = val("host"), val("name")
if cmd == "store":
    data = sys.stdin.read()
    items = [r for r in items if not (r["host"] == host and r["name"] == name)]
    items.append({"host": host, "name": name, "value": data})
    save(items)
elif cmd == "lookup":
    for r in items:
        if r["host"] == host and r["name"] == name:
            sys.stdout.write(r["value"])
            sys.exit(0)
    sys.exit(1)
elif cmd == "clear":
    save([r for r in items if not (r["host"] == host and r["name"] == name)])
elif cmd == "search":
    for r in items:
        if r["host"] == host:
            sys.stderr.write("attribute.name = %s\n" % r["name"])
sys.exit(0)
'''


@contextlib.contextmanager
def temp_config(text: str = ""):
    """Point hosts.SSH_CONFIG at a temp file holding `text`, and restore it."""
    was = hosts.SSH_CONFIG
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config"
        path.write_text(text)
        hosts.SSH_CONFIG = path
        try:
            yield path
        finally:
            hosts.SSH_CONFIG = was


def _raised(call) -> str:
    """The message a HostError carried, or "" if the call did not raise one."""
    try:
        call()
    except hosts.HostError as exc:
        return str(exc)
    return ""


def structurally_sound(text: str) -> bool:
    """A stand-in for "ssh can still parse it", without running ssh: every Host
    or Match header opens at the left margin, and no directive floats indented
    above the first block. Catches an edit that unindented a header or let a
    line escape its block -- the ways a rewrite breaks the file's structure."""
    seen_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        first = stripped.split()[0].lower()
        if first in ("host", "match"):
            if indented:
                return False
            seen_header = True
        elif indented and not seen_header:
            return False
    return True


def config_read_checks(check: Checks) -> None:
    with temp_config(RICH):
        web = hosts.host_config("web-1")
        check("a block's own directives come back as written",
              web == {"hostname": "10.0.0.1", "user": "deploy",
                      "port": "2222", "identityfile": ""},
              f"got={web}")
        check("the Host * IdentityFile is not folded into a block that has "
              "none of its own", web["identityfile"] == "")
        db = hosts.host_config("shared-db")
        check("a shared Host line reads the same block from either alias",
              db == hosts.host_config("db")
              and db["hostname"] == "10.0.0.2", f"got={db}")
        check("a name the file does not carry is refused",
              _raised(lambda: hosts.host_config("ghost")))
        check("and so is the wildcard, which config_hosts never lists",
              _raised(lambda: hosts.host_config("*")))

        check("the other aliases on a shared line are surfaced",
              hosts.host_line_aliases("db") == ["shared-db"]
              and hosts.host_line_aliases("shared-db") == ["db"])
        check("a block that stands alone shares its line with nobody",
              hosts.host_line_aliases("web-1") == [])
        check("aliases of a name not present is refused",
              _raised(lambda: hosts.host_line_aliases("ghost")))

    # A probe that reaches ssh but never opens the session: three problems,
    # three states -- and a new server's untrusted key is its own first-contact
    # state, not a dead "down".
    state, why = hosts._probe_failure("Host key verification failed.", "box")
    check("an untrusted host key is 'unverified', not 'down'",
          state == "unverified" and "log in" in why, f"state={state} why={why!r}")
    state, _ = hosts._probe_failure("Permission denied (publickey).", "box")
    check("being reached and refused is 'nokey'", state == "nokey")
    state, _ = hosts._probe_failure("Permission denied (publickey).", "local")
    check("but a refusal from this machine is never a key problem",
          state == "down")
    state, _ = hosts._probe_failure("ssh: could not resolve hostname box", "box")
    check("and an unreachable box is plainly 'down'", state == "down")


def update_host_checks(check: Checks) -> None:
    with temp_config(RICH) as path:
        before = path.read_text()
        backup = hosts.update_host("web-1", {"hostname": "192.168.1.5",
                                             "user": "", "port": "",
                                             "identityfile": "~/.ssh/web1_key"})
        text = path.read_text()
        check("a replace, an insert and two removes land together",
              hosts.host_config("web-1") == {"hostname": "192.168.1.5",
                                             "user": "", "port": "",
                                             "identityfile": "~/.ssh/web1_key"},
              f"got={hosts.host_config('web-1')}")
        check("the replaced directive keeps its indent",
              "\n    HostName 192.168.1.5\n" in text)
        check("the inserted directive is written like add_host would",
              "\n    IdentityFile ~/.ssh/web1_key\n" in text)
        check("the removed directives are gone",
              "User deploy" not in text and "Port 2222" not in text)
        check("comments and unknown directives in the block survive",
              "# note about this host" in text
              and "    ProxyCommand corkscrew proxy 8080 %h %p" in text)
        check("the Include and the Match block are untouched",
              "Include ~/.ssh/extra" in text
              and "Match host bastion" in text
              and "    ForwardAgent yes" in text)
        check("the Host * defaults are untouched",
              "Host *" in text and "    ServerAliveInterval 30" in text
              and "    IdentityFile ~/.ssh/id_ed25519" in text)
        check("the file still parses as ssh config",
              structurally_sound(text))
        check("every alias is still named",
              hosts.config_hosts() == ["web-1", "db", "shared-db"],
              f"got={hosts.config_hosts()}")
        check("the backup holds what was there before, and the file stays 0600",
              backup.read_text() == before
              and (path.stat().st_mode & 0o777) == 0o600)

    # Insert vs replace, in isolation.
    with temp_config("Host m\n    HostName x\n") as path:
        hosts.update_host("m", {"user": "bob"})
        check("setting a directive the block lacks inserts it",
              hosts.host_config("m")["user"] == "bob"
              and path.read_text().count("    User ") == 1)
        hosts.update_host("m", {"user": "carol"})
        check("setting one it already has replaces in place, not twice",
              hosts.host_config("m")["user"] == "carol"
              and path.read_text().count("    User ") == 1,
              f"text={path.read_text()!r}")

    # Several inserts at once arrive in a fixed order, under the Host line.
    with temp_config("Host m\n    HostName x\n") as path:
        hosts.update_host("m", {"port": "2200", "user": "bob",
                               "identityfile": "~/.ssh/mkey"})
        check("new directives are inserted in a stable order under the header",
              "Host m\n    User bob\n    Port 2200\n"
              "    IdentityFile ~/.ssh/mkey\n    HostName x\n"
              in path.read_text(), f"text={path.read_text()!r}")

    # Removing a directive with an empty value.
    with temp_config("Host m\n    HostName x\n    User bob\n") as path:
        hosts.update_host("m", {"user": ""})
        check("an empty value removes the directive",
              hosts.host_config("m")["user"] == ""
              and "User" not in path.read_text())

    # A hand-authored block can repeat a directive; ssh takes the first, so an
    # edit that touched only the first would leave a stale copy in charge.
    with temp_config("Host m\n    HostName x\n    HostName y\n    User bob\n") as path:
        hosts.update_host("m", {"hostname": ""})
        check("clearing a duplicated directive removes every copy",
              "HostName" not in path.read_text()
              and "User bob" in path.read_text(),
              f"text={path.read_text()!r}")
    with temp_config("Host m\n    IdentityFile ~/a\n    IdentityFile ~/b\n") as path:
        hosts.update_host("m", {"identityfile": "~/c"})
        check("setting a duplicated directive collapses it to one",
              path.read_text().count("IdentityFile") == 1
              and hosts.host_config("m")["identityfile"] == "~/c",
              f"text={path.read_text()!r}")

    # Port 22 and empty both mean "drop the Port line".
    with temp_config("Host m\n    HostName x\n    Port 2222\n") as path:
        hosts.update_host("m", {"port": "22"})
        check("Port 22 is the default and is removed, not written",
              "Port" not in path.read_text())
        check("removing a Port that is already absent is a quiet no-op",
              _raised(lambda: hosts.update_host("m", {"port": "22"})) == ""
              and "Port" not in path.read_text())

    # Editing through a shared alias edits the one shared block.
    with temp_config("Host db shared-db\n    HostName 10.0.0.2\n") as path:
        hosts.update_host("shared-db", {"user": "admin"})
        check("an edit through one alias reaches the block both share",
              hosts.host_config("db")["user"] == "admin"
              and hosts.host_config("shared-db")["user"] == "admin")

    # Bad values are refused before the file is opened.
    with temp_config(RICH) as path:
        before = path.read_text()
        for bak in path.parent.glob("config.bak.*"):
            bak.unlink()
        bad = {"a space in the host": {"hostname": "bad host"},
               "a hostname leading with a dash": {"hostname": "-nope"},
               "a shell metacharacter in the user": {"user": "we;rm"},
               "a non-numeric port": {"port": "22a"},
               "a newline in the identity file": {
                   "identityfile": "~/.ssh/k\nProxyCommand evil"}}
        refused = all(_raised(lambda f=f: hosts.update_host("web-1", f))
                      for f in bad.values())
        check("every unsafe value is refused with HostError",
              refused, f"labels={list(bad)}")
        check("a refused edit leaves the file and its backups alone",
              path.read_text() == before
              and "ProxyCommand evil" not in path.read_text()
              and list(path.parent.glob("config.bak.*")) == [])
        check("editing a host that is not there is refused",
              _raised(lambda: hosts.update_host("ghost", {"user": "x"})))


def rename_host_checks(check: Checks) -> None:
    with temp_config("Host db shared-db\n    HostName 10.0.0.2\n"
                     "    User admin\n\nHost web-1\n"
                     "    HostName 10.0.0.1\n") as path:
        backup = hosts.rename_host("shared-db", "replica")
        text = path.read_text()
        check("the old token is swapped and the other alias stays",
              "Host db replica\n" in text and "shared-db" not in text)
        check("the block's directives are left untouched",
              "    HostName 10.0.0.2" in text and "    User admin" in text)
        check("the config now names the new alias, not the old",
              "replica" in hosts.config_hosts()
              and "shared-db" not in hosts.config_hosts())
        check("the shared block is still reachable from the new name",
              hosts.host_line_aliases("replica") == ["db"])
        check("it still parses, and the backup and mode are as for any edit",
              structurally_sound(text) and backup.read_text()
              and (path.stat().st_mode & 0o777) == 0o600)

        check("renaming onto a name already in the config is refused",
              _raised(lambda: hosts.rename_host("db", "web-1")))
        check("renaming onto the reserved 'local' is refused too",
              _raised(lambda: hosts.rename_host("db", "local")))
        check("an invalid new name is refused",
              _raised(lambda: hosts.rename_host("db", "-bad"))
              and _raised(lambda: hosts.rename_host("db", "two words")))
        check("renaming a host that is not there is refused",
              _raised(lambda: hosts.rename_host("ghost", "fresh")))
        check("a refused rename changed nothing",
              path.read_text() == text)


def migrate_secrets_checks(check: Checks) -> None:
    """migrate_secrets end to end against a fake secret-tool on PATH -- the one
    function that talks to the keyring, never let near the real one."""
    with tempfile.TemporaryDirectory() as tmp:
        fakebin = Path(tmp) / "bin"
        fakebin.mkdir()
        store = Path(tmp) / "store"
        store.mkdir()
        tool = fakebin / "secret-tool"
        tool.write_text(FAKE_SECRET_TOOL)
        tool.chmod(0o755)
        was_path = os.environ["PATH"]
        was_kr = os.environ.get("FAKE_KEYRING")
        os.environ["PATH"] = f"{fakebin}:{was_path}"
        os.environ["FAKE_KEYRING"] = str(store)
        try:
            hosts.secret_store("old-box", "password", "s3cr3t")
            hosts.secret_store("old-box", "api-token", "tok-42")
            check("the fake keyring files and lists what it is given",
                  hosts.secret_names("old-box") == ["api-token", "password"],
                  f"got={hosts.secret_names('old-box')}")
            hosts.migrate_secrets("old-box", "new-box")
            check("migrate re-files every secret under the new host",
                  hosts.secret_names("new-box") == ["api-token", "password"]
                  and hosts.secret_value("new-box", "password") == "s3cr3t"
                  and hosts.secret_value("new-box", "api-token") == "tok-42",
                  f"got={hosts.secret_names('new-box')}")
            check("and clears them from the old host",
                  hosts.secret_names("old-box") == [])
            check("a host with nothing filed migrates without error",
                  _raised(lambda: hosts.migrate_secrets("empty", "other")) == ""
                  and hosts.secret_names("other") == [])
        finally:
            os.environ["PATH"] = was_path
            if was_kr is None:
                os.environ.pop("FAKE_KEYRING", None)
            else:
                os.environ["FAKE_KEYRING"] = was_kr


def main() -> int:
    check = Checks()
    frame_checks(check)
    capture_checks(check)
    mounts_table_checks(check)
    mount_action_checks(check)
    ssh_mount_script_checks(check)
    config_read_checks(check)
    update_host_checks(check)
    rename_host_checks(check)
    migrate_secrets_checks(check)

    gui = load_gui()
    if gui is None:
        print("skipped: notification and stream checks need the GTK stack")
    else:
        notification_checks(check, gui)
        stream_checks(check, gui)
        drain_checks(check, gui)

    print(f"\n{'all good' if not check.failures else str(check.failures) + ' failed'}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
