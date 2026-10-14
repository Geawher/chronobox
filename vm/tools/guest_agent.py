"""Guest agent inside a chronobox Firecracker VM.

Listens on vsock port 5000. Each connection carries one JSON request line and
gets one JSON reply line:

    {"op": "ping"}                                  -> {"ok": true}
    {"op": "run", "command": "...", "timeout": 60}  -> {"code": 0, "output": "..."}
    {"op": "run", ..., "isolated": true}            -> same

The agent runs from the tools drive; commands run inside the machine's image
(mounted at /newroot) through chroot, with the image's shell (bash if it has
one, else sh) and the environment and working directory from its Docker config
(``/.chronobox/image.json`` in the image, written by ``chronobox build``). The
working directory and exported variables persist between commands (saved in
/root/.chronobox, so they are part of every snapshot), so these commands run one
at a time. Background processes keep running: the whole VM (memory and
processes) is what chronobox snapshots.

Isolated commands are for observers (health probes, checkers): they run next to
the others, in a private mount namespace, without the shared state, and read
their script from stdin, so it never shows in the process list or environment.
Mounts they make are invisible to every other process.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

PORT = 5000
ROOT = Path("/newroot")
IMAGE_CONFIG = ROOT / ".chronobox" / "image.json"
SHELLS = ("/bin/bash", "/bin/sh")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_WORKDIR = "/root"
STATE = "/root/.chronobox"
# The command arrives in CHRONOBOX_CMD and leaves the environment before it runs: saved
# exports must never hold it (dash and busybox print a multi-line value over several lines,
# which no line filter can drop, and sourcing them would run its tail again).
WRAPPER = """
S={state}
mkdir -p $S
cd "$(cat $S/cwd 2>/dev/null || echo {workdir})" 2>/dev/null
[ -f $S/env ] && . $S/env >/dev/null 2>&1
__chronobox_cmd=$CHRONOBOX_CMD
unset CHRONOBOX_CMD
eval "$__chronobox_cmd" </dev/null
__rc=$?
pwd > $S/cwd; export -p > $S/env
exit $__rc
"""
shared_state = threading.Lock()


def load_image_config() -> dict:
    try:
        return json.loads(IMAGE_CONFIG.read_text())
    except FileNotFoundError:
        return {}


def pick_shell() -> str:
    """The image's shell: bash if it has one, else sh.

    The image's own files decide, so look under the image root, not on this drive; lexists,
    because an absolute symlink (e.g. /bin/sh -> /usr/bin/dash) only resolves inside the image.

    Raises:
        SystemExit: If the image has neither (``chronobox build`` refuses such images).
    """
    shell = next((shell for shell in SHELLS if os.path.lexists(ROOT / shell.lstrip("/"))), None)
    if shell is None:
        raise SystemExit(f"chronobox guest agent: the image has no shell ({', '.join(SHELLS)})")
    return shell


IMAGE = load_image_config()
SHELL = pick_shell()
WORKDIR = IMAGE.get("workdir") or DEFAULT_WORKDIR
ENV = {"PATH": DEFAULT_PATH, **IMAGE.get("env", {}), "HOME": "/root", "TERM": "dumb"}
SHARED = ["chroot", str(ROOT), SHELL, "-c", WRAPPER.format(state=STATE, workdir=shlex.quote(WORKDIR))]
ISOLATED = ["unshare", "--mount", "--propagation", "private", "chroot", str(ROOT), SHELL, "-s"]


def run(command: str, timeout: float, *, isolated: bool = False) -> dict:
    if isolated:
        # The script is a single braced group: the shell parses all of it before running it,
        # so nothing it starts can read the rest of the script from stdin.
        return execute(ISOLATED, ENV, timeout, script=f"cd /root\n{{\n{command}\n}} </dev/null\n")
    with shared_state:
        return execute(SHARED, {**ENV, "CHRONOBOX_CMD": command}, timeout, script=None)


def execute(argv: list[str], env: dict[str, str], timeout: float, *, script: str | None) -> dict:
    # Output goes to a file, not a pipe: background services started by the
    # command keep the descriptor open, and we must not wait for them.
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(
            argv, env=env, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL if script is None else subprocess.PIPE, start_new_session=True,
        )
        try:
            if script is not None:
                process.stdin.write(script.encode())
                process.stdin.close()
            code = process.wait(timeout=timeout)
            suffix = ""
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code, suffix = 124, f"\n[timed out after {timeout:g}s]"
        out.seek(0)
        return {"code": code, "output": out.read().decode(errors="replace") + suffix}


def handle(connection: socket.socket) -> None:
    data = b""
    while not data.endswith(b"\n"):
        chunk = connection.recv(65536)
        if not chunk:
            break
        data += chunk
    request = json.loads(data or b"{}")
    if request.get("op") == "run":
        isolated = bool(request.get("isolated"))
        reply = run(request["command"], float(request.get("timeout", 120)), isolated=isolated)
    else:
        reply = {"ok": True}
    connection.sendall((json.dumps(reply) + "\n").encode())


def serve(connection: socket.socket) -> None:
    try:
        handle(connection)
    except Exception as error:  # never let one request kill the agent
        try:
            connection.sendall((json.dumps({"code": -1, "output": repr(error)}) + "\n").encode())
        except OSError:
            pass
    finally:
        connection.close()


def main() -> None:
    os.environ.setdefault("PATH", DEFAULT_PATH)
    server = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    server.bind((socket.VMADDR_CID_ANY, PORT))
    server.listen(16)
    print("chronobox guest agent ready", flush=True)
    while True:
        connection, _ = server.accept()
        threading.Thread(target=serve, args=(connection,), daemon=True).start()


if __name__ == "__main__":
    main()
