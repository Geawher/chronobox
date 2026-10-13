"""Guest agent inside a chronobox Firecracker VM.

Listens on vsock port 5000. Each connection carries one JSON request line and
gets one JSON reply line; ``put`` and ``get`` add a raw tar archive after the
line (its length is in the line):

    {"op": "ping"}                                  -> {"ok": true}
    {"op": "run", "command": "...", "timeout": 60}  -> {"code": 0, "output": "..."}
    {"op": "run", ..., "isolated": true}            -> same
    {"op": "put", "path": "/src", "size": N} + tar  -> {"ok": true}
    {"op": "get", "path": "/src", "exclude": [...]} -> {"size": N} + tar
    {"op": "clock", "epoch": 1791900000.5}          -> {"ok": true}

Any failure comes back as {"error": "..."} (``run`` reports command failures in
its exit code instead).

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

Files move as tar archives, unpacked and packed by a child process that has
entered the image with chroot: paths and symlinks resolve inside the image, and
the image needs no tar of its own. Unpacked files belong to root.
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

PORT = 5000
ROOT = Path("/newroot")
IMAGE_CONFIG = ROOT / ".chronobox" / "image.json"
SHELLS = ("/bin/bash", "/bin/sh")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_WORKDIR = "/root"
STATE = "/root/.chronobox"
WRAPPER = """
S={state}
mkdir -p $S
cd "$(cat $S/cwd 2>/dev/null || echo {workdir})" 2>/dev/null
[ -f $S/env ] && . $S/env >/dev/null 2>&1
eval "$CHRONOBOX_CMD" </dev/null
__rc=$?
pwd > $S/cwd; export -p | grep -v ' CHRONOBOX_CMD=' > $S/env
exit $__rc
"""
CHILD_FAILED = 1
shared_state = threading.Lock()


class Op(StrEnum):
    PING = "ping"
    RUN = "run"
    PUT = "put"
    GET = "get"
    CLOCK = "clock"


class AgentError(Exception):
    """A request could not be done; the message goes back to the host."""


def load_image_config() -> dict:
    try:
        return json.loads(IMAGE_CONFIG.read_text())
    except FileNotFoundError:
        return {}


def pick_shell() -> str:
    # The image's own files decide, so look under the image root, not on this drive.
    return next(shell for shell in SHELLS if (ROOT / shell.lstrip("/")).exists())


IMAGE = load_image_config()
SHELL = pick_shell()
WORKDIR = IMAGE.get("workdir") or DEFAULT_WORKDIR
ENV = {"PATH": DEFAULT_PATH, **IMAGE.get("env", {}), "HOME": "/root", "TERM": "dumb"}
SHARED = ["chroot", str(ROOT), SHELL, "-c", WRAPPER.format(state=STATE, workdir=WORKDIR)]
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


def in_image(work: Callable[[], bytes]) -> bytes:
    """Run ``work`` in a child process chrooted into the image; return what it returns.

    Raises:
        AgentError: With the child's error message, if ``work`` raised.
    """
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        code = 0
        with os.fdopen(write_end, "wb") as pipe:
            try:
                os.chroot(ROOT)
                os.chdir("/")
                pipe.write(work())
            except Exception as error:  # every failure goes back to the parent as text
                pipe.write(f"{type(error).__name__}: {error}".encode())
                code = CHILD_FAILED
        os._exit(code)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as pipe:
        result = pipe.read()
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise AgentError(result.decode(errors="replace"))
    return result


def owned_by_root(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    return tarfile.tar_filter(member, destination).replace(uid=0, gid=0, uname="root", gname="root")


def put(path: str, archive: bytes) -> None:
    def unpack() -> bytes:
        os.makedirs(path, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(path, filter=owned_by_root)
        return b""

    in_image(unpack)


def is_excluded(relative: str, patterns: list[str]) -> bool:
    """A pattern without "/" matches any path component; one with "/" a path and what is under it."""
    parts = relative.split("/")
    for pattern in patterns:
        if "/" in pattern:
            prefix = pattern.strip("/")
            if fnmatch.fnmatch(relative, prefix) or relative.startswith(prefix + "/"):
                return True
        elif any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def get(path: str, exclude: list[str]) -> bytes:
    def skip_excluded(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        relative = member.name.removeprefix("./")
        return None if relative != "." and is_excluded(relative, exclude) else member

    def pack() -> bytes:
        if not os.path.lexists(path):
            raise FileNotFoundError(path)
        # A folder's content goes in as "./...", a single file under its own name.
        name = "." if os.path.isdir(path) else os.path.basename(path)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            tar.add(path, arcname=name, filter=skip_excluded)
        return buffer.getvalue()

    return in_image(pack)


def set_clock(epoch: float) -> None:
    time.clock_settime(time.CLOCK_REALTIME, epoch)


def answer(request: dict, stream: BinaryIO) -> tuple[dict, bytes]:
    """The reply line and the raw bytes that follow it (empty for most ops)."""
    op = request.get("op", Op.PING)
    if op == Op.RUN:
        isolated = bool(request.get("isolated"))
        return run(request["command"], float(request.get("timeout", 120)), isolated=isolated), b""
    if op == Op.PUT:
        put(request["path"], stream.read(int(request["size"])))
        return {"ok": True}, b""
    if op == Op.GET:
        archive = get(request["path"], list(request.get("exclude", [])))
        return {"size": len(archive)}, archive
    if op == Op.CLOCK:
        set_clock(float(request["epoch"]))
        return {"ok": True}, b""
    return {"ok": True}, b""


def serve(connection: socket.socket) -> None:
    try:
        with connection.makefile("rb") as stream:
            request = json.loads(stream.readline() or b"{}")
            try:
                reply, payload = answer(request, stream)
            except AgentError as error:
                reply, payload = {"error": str(error)}, b""
        connection.sendall((json.dumps(reply) + "\n").encode() + payload)
    except Exception as error:  # never let one request kill the agent
        failure = {"code": -1, "error": repr(error), "output": repr(error)}
        with contextlib.suppress(OSError):
            connection.sendall((json.dumps(failure) + "\n").encode())
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
