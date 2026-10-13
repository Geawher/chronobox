# chronobox

**A Linux machine with exact undo and forks, for coding agents — as an MCP server.**

**Let an agent test its reasoning on a copy of reality before it acts.** When an agent
works on a live system, each step of its plan is a guess: "this migration is safe under
traffic", "this setting stops the out-of-memory kills". Usually the only way to check a
guess is to run it in production. With chronobox the agent forks the running machine,
runs the step on the exact copy, watches what happens, and changes the real machine only
once the copy shows it works. If something still goes wrong, it can roll the whole
machine back. In our benchmark this made models safer, and let a cheaper model match a
stronger one ([below](#does-it-help)).

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

## Does it help?

We measured it on [chronobox-ops](https://github.com/Geawher/chronobox-ops), our benchmark
of 34 tasks on live production machines. Each task has a hazard a careful engineer
avoids: traffic that must keep flowing, writes that must not be lost, a setting that only
looks safe. Claude Code with chronobox, compared with the same Claude Code alone:

- **A cheaper model matches a stronger one.** Sonnet 5.5 with chronobox does the task
  on 86% of runs and harms users on 12%, against 82% and 13% for Opus 4.8 alone, at
  $0.13 per run instead of $1.09. The 95% intervals of the differences, [−3, +13] and
  [−7, +5] points, leave no gap we can measure.
- **The same model is safer.** Sonnet 5.5 succeeds on 14 points more runs and harms users
  half as often (72% → 86%; 26% → 12%), both outside chance. Sonnet 5 (+11 points) and
  Opus 4.8 (+7) gain too.
- **The strongest model does not need it.** Opus 5.5 succeeds on 96% of runs alone, often
  by building its own test copy, and gains nothing.

The benchmark is ours, and its tasks are built around such hazards: on routine work with
nothing to break, expect little difference. Full results, per-task tables and method:
[chronobox-ops results](https://github.com/Geawher/chronobox-ops/blob/main/results/README.md).

## Requirements

Linux with KVM (read/write access to `/dev/kvm`, e.g. membership of the `kvm`
group), Python 3.12 with [uv](https://docs.astral.sh/uv/), and Docker to build
images. No root needed. macOS and Windows are not supported (Firecracker needs
KVM); a Linux VM with nested virtualization or a remote Linux box works.

## Quickstart

```bash
git clone https://github.com/Geawher/chronobox && cd chronobox
uv sync
vm/fetch.sh            # Firecracker + guest kernel into vm/build
vm/build_rootfs.sh     # the tools drive + the default image (Debian: Python, PostgreSQL, nginx...)
uv run pytest          # includes real-VM tests

claude mcp add chronobox -- uv run --project "$PWD" chronobox-mcp
```

Each Claude Code session then gets a fresh machine (boot ≈ 1 s). Any MCP client
works the same way.

The VM files go to `vm/build` of the checkout. To put them elsewhere (or when chronobox
is installed as a package, e.g. by chronobox-ops), set `CHRONOBOX_VM_DIR` for both the
build scripts and chronobox; an installed chronobox without it looks in
`~/.local/share/chronobox/vm`.

### Your own image

Any Docker image can be the machine's system, from a registry or from your Dockerfile:

```bash
uv run chronobox build --image postgres:16                      # -> image "postgres-16"
uv run chronobox build --dockerfile ./Dockerfile --name myapp   # context: the Dockerfile's folder
uv run chronobox images
claude mcp add chronobox -- uv run --project "$PWD" chronobox-mcp --image myapp
```

The image needs nothing from chronobox (no Python, no init): commands run inside it
with its own shell (bash if it has one, else `sh`), its `ENV` and its `WORKDIR`, as
root. Nothing is started automatically; start the image's services from `--setup` or
let the agent do it (e.g. `docker-entrypoint.sh postgres &` in the postgres image).

Options: `--image NAME` (default `default`), `--profile alone|rollback|chronobox`
(default `chronobox`), `--guard`,
`--setup script.sh` (prepare the machine at boot: install, start services, load
data), `--mem`, `--disk`.

## Tools

| Tool | Profiles | What it does |
|---|---|---|
| `run(command, where="main")` | all | one bash command as root on the main machine or a fork |
| `wait(seconds, where)` | all | let time pass while services and traffic keep running |
| `checkpoint(label, note)` | rollback, chronobox | save the whole main machine |
| `list_checkpoints()` | rollback, chronobox | checkpoints, lessons, open forks |
| `rollback(label, lesson)` | rollback, chronobox | restore the main machine exactly (users' writes since the checkpoint are lost too); record why |
| `fork(name, from_checkpoint)` | chronobox | independent copy of the live machine |
| `compare(command, forks)` | chronobox | the same command on main and forks, side by side |
| `changes(name)` | chronobox | commands, failures and files a fork changed |
| `promote(name)` / `discard(name, lesson)` | chronobox | keep a fork as main / drop it, with a report |

With `--profile chronobox`, risky commands (deletes, kills, restarts, SQL changes,
overwrites) are checkpointed automatically first.

The tool descriptions steer the agent to the safe pattern on a machine that serves
users: fork, try the change and let time pass on the fork, then apply the verified
change to the main machine. A rollback is exact, so it also undoes the writes users
made since the checkpoint; on a machine that takes writes, fixing forward is usually
better.

## Long-lived machines

`chronobox-mcp --serve machine.sock` owns the VM and serves it on a unix socket;
`chronobox-mcp --connect machine.sock` is a thin MCP front end (stdio, or HTTP with
`--http HOST:PORT`, e.g. for an agent in a container). The machine outlives agent
sessions, and harnesses can talk to it directly (`chronobox.request`) — this is how
the chronobox-ops benchmark measures what agents do.

## How it works

Each machine is a Firecracker microVM that boots a small shared **tools drive**
(init and guest agent, Alpine), mounts the machine's **image** (read-only) under a
sparse writable disk (overlay), and runs the agent's commands inside the image with
chroot; the guest agent is reached over vsock. A snapshot
pauses the VM for well under a second, saves memory and device state plus the
changed disk blocks, and resumes it; restoring starts a new VM from those files.
Only running machines (main + open forks) use RAM, and guest memory is allocated
lazily; checkpoints are files on disk.

| Operation | Typical time |
|---|---|
| boot | ~1 s |
| snapshot (checkpoint / fork point) | 0.3–0.7 s |
| restore (rollback / fork) | 30–60 ms |

## Limits

- Linux + KVM only; VMs have no network (everything a task needs runs inside).
- Command classification (risky / needs rehearsal) is a heuristic over command text.
- Large environments: each live fork needs the RAM its services use; prefer short
  forks, or a bigger remote Linux machine.

## Layout

```
src/chronobox/   vm.py (Firecracker backend) · workspace.py (main, checkpoints, forks,
                 reports, lessons, guard) · commands.py · service.py · server.py (MCP)
                 images.py · cli.py (`chronobox build` / `images`)
vm/              tools/ (tools drive: init, guest agent) · images/default/ · fetch.sh · build_rootfs.sh
tests/           unit tests + real-VM tests (skipped without /dev/kvm)
```

## License

Apache-2.0, see [LICENSE](LICENSE).
