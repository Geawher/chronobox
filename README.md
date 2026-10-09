# chronobox

**A Linux machine with exact undo and forks, for coding agents — as an MCP server.**

Agents that work on real systems (upgrade a dependency, migrate a database, change
a live config) make mistakes that cannot be undone with `git`: data is gone, a
service restarted and lost its in-memory state, clients were told their writes
succeeded. chronobox gives the agent a machine where those mistakes are cheap:

- **checkpoint / rollback** the whole machine — disk, memory, running processes,
  open database transactions — restored in ~50 ms;
- **fork** the live machine into exact copies, try a risky change there while the
  real machine keeps serving, let time pass, compare, then apply the verified change
  (or promote the copy, or throw it away);
- **reports and lessons**: discarding a fork tells the agent what it changed and that
  the main machine is still untouched; lessons come back on later forks and rollbacks;
- an optional **rehearsal guard**: destructive commands on the main machine are refused
  until they succeeded on a fork (or are forced).

There is no search or autopilot inside: the agent decides when to use the tools.

## Requirements

Linux with KVM (read/write access to `/dev/kvm`, e.g. membership of the `kvm`
group), Python 3.12 with [uv](https://docs.astral.sh/uv/), and Docker once to build
the VM image. No root needed. macOS and Windows are not supported (Firecracker needs
KVM); a Linux VM with nested virtualization or a remote Linux box works.

## Quickstart

```bash
git clone https://github.com/Geawher/chronobox && cd chronobox
uv sync
vm/fetch.sh            # Firecracker + guest kernel into vm/build
vm/build_rootfs.sh     # Debian root disk: Python, PostgreSQL, nginx, git, sqlite, uv...
uv run pytest          # includes real-VM tests

claude mcp add chronobox -- uv run --project "$PWD" chronobox-mcp
```

Each Claude Code session then gets a fresh machine (boot ≈ 1 s). Any MCP client
works the same way.

The VM image goes to `vm/build` of the checkout. To put it elsewhere (or when chronobox
is installed as a package, e.g. by chronobox-ops), set `CHRONOBOX_VM_DIR` for both the
build scripts and chronobox; an installed chronobox without it looks in
`~/.local/share/chronobox/vm`.

Options: `--profile alone|rollback|chronobox` (default `chronobox`), `--guard`,
`--setup script.sh` (prepare the machine at boot: install, start services, load
data), `--mem`, `--disk`.

## Tools

| Tool | Profiles | What it does |
|---|---|---|
| `run(command, where="main")` | all | one bash command as root on the main machine or a fork |
| `wait(seconds, where)` | all | let time pass while services and traffic keep running |
| `checkpoint(label, note)` | rollback, chronobox | save the whole main machine |
| `list_checkpoints()` | rollback, chronobox | checkpoints, lessons, open forks |
| `rollback(label, lesson)` | rollback, chronobox | restore the main machine exactly; record why |
| `fork(name, from_checkpoint)` | chronobox | independent copy of the live machine |
| `compare(command, forks)` | chronobox | the same command on main and forks, side by side |
| `changes(name)` | chronobox | commands, failures and files a fork changed |
| `promote(name)` / `discard(name, lesson)` | chronobox | keep a fork as main / drop it, with a report |

With `--profile chronobox`, risky commands (deletes, kills, restarts, SQL changes,
overwrites) are checkpointed automatically first.

## Long-lived machines

`chronobox-mcp --serve machine.sock` owns the VM and serves it on a unix socket;
`chronobox-mcp --connect machine.sock` is a thin MCP front end (stdio, or HTTP with
`--http HOST:PORT`, e.g. for an agent in a container). The machine outlives agent
sessions, and harnesses can talk to it directly (`chronobox.request`) — this is how
the chronobox-ops benchmark measures what agents do.

## How it works

Each machine is a Firecracker microVM: a shared read-only root disk, a sparse
writable disk on top (overlay), and a guest agent reached over vsock. A snapshot
pauses the VM for well under a second, saves memory and device state plus the
changed disk blocks, and resumes it; restoring starts a new VM from those files.
Only running machines (main + open forks) use RAM, and guest memory is allocated
lazily; checkpoints are files on disk.

| Operation | Typical time |
|---|---|
| boot | ~1 s |
| snapshot (checkpoint / fork point) | 0.3–0.7 s |
| restore (rollback / fork) | 30–60 ms |

## Evidence: what it does and does not do

Measured on chronobox-ops (30 live-system tasks, 800+ Claude Code runs):

- with normal Claude Code, Sonnet solved 40/42 tasks with chronobox vs 33/42 without
  (p = 0.048; 16/18 vs 9/18 on the hardest tasks), at equal safety;
- whole-machine rollback alone caused the most catastrophic runs (clients' writes
  thrown away); chronobox gives undo without that damage;
- Opus and easy tasks gain nothing; it adds 30–50 % wall time.

## Limits

- Linux + KVM only; VMs have no network (everything a task needs runs inside).
- Command classification (risky / needs rehearsal) is a heuristic over command text.
- Large environments: each live fork needs the RAM its services use; prefer short
  forks, or a bigger remote Linux machine.

## Layout

```
src/chronobox/   vm.py (Firecracker backend) · workspace.py (main, checkpoints, forks,
                 reports, lessons, guard) · commands.py · service.py · server.py (MCP)
vm/              image (Dockerfile, init, guest agent) · fetch.sh · build_rootfs.sh
tests/           unit tests + real-VM tests (skipped without /dev/kvm)
```

## License

Apache-2.0, see [LICENSE](LICENSE).
