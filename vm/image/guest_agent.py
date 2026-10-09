"""Guest agent inside a chronobox Firecracker VM.

Listens on vsock port 5000. Each connection carries one JSON request line and
gets one JSON reply line:

    {"op": "ping"}                                  -> {"ok": true}
    {"op": "run", "command": "...", "timeout": 60}  -> {"code": 0, "output": "..."}
    {"op": "run", ..., "isolated": true}            -> same

Commands run with bash; the working directory and exported variables persist
between commands (saved in /root/.chronobox, so they are part of every
snapshot), so these commands run one at a time. Background processes keep
running: the whole VM (memory and processes) is what chronobox snapshots.

Isolated commands are for observers (health probes, checkers): they run next to
the others, in a private mount namespace, without the shared state, and read
their script from stdin, so it never shows in the process list or environment.
Mounts they make are invisible to every other process.
"""

import json
import os
import socket
import subprocess
import tempfile
import threading

PORT = 5000
STATE = "/root/.chronobox"
WRAPPER = f"""
S={STATE}
mkdir -p $S
cd "$(cat $S/cwd 2>/dev/null || echo /root)" 2>/dev/null
[ -f $S/env ] && . $S/env >/dev/null 2>&1
eval "$CHRONOBOX_CMD" </dev/null
__rc=$?
pwd > $S/cwd; export -p | grep -v ' CHRONOBOX_CMD=' > $S/env
exit $__rc
"""
ISOLATED = ["unshare", "--mount", "--propagation", "private", "bash", "-s"]
shared_state = threading.Lock()


def run(command: str, timeout: float, *, isolated: bool = False) -> dict:
    env = {**os.environ, "HOME": "/root", "TERM": "dumb"}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    if isolated:
        # The script is a single braced group: bash parses all of it before running it,
        # so nothing it starts can read the rest of the script from stdin.
        return execute(ISOLATED, env, timeout, script=f"cd /root\n{{\n{command}\n}} </dev/null\n")
    with shared_state:
        return execute(["bash", "-c", WRAPPER], {**env, "CHRONOBOX_CMD": command}, timeout, script=None)


def execute(argv: list, env: dict, timeout: float, *, script: str | None) -> dict:
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
    os.environ.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    server = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    server.bind((socket.VMADDR_CID_ANY, PORT))
    server.listen(16)
    print("chronobox guest agent ready", flush=True)
    while True:
        connection, _ = server.accept()
        threading.Thread(target=serve, args=(connection,), daemon=True).start()


if __name__ == "__main__":
    main()
