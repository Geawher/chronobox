# How chronobox works

## Machines and snapshots

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

## Long-lived machines

`chronobox-mcp --serve machine.sock` owns the VM and serves it on a unix socket;
`chronobox-mcp --connect machine.sock` is a thin MCP front end (stdio, or HTTP with
`--http HOST:PORT`, e.g. for an agent in a container; the URL's path carries `--http-token`, a
random one by default, printed at start). The machine outlives agent
sessions, and harnesses can talk to it directly (`chronobox.request`) — this is how
the chronobox-ops benchmark measures what agents do.

## Limits

- Linux + KVM only; VMs have no network (everything a task needs runs inside).
- Command classification (risky / needs rehearsal) is a heuristic over command text.
- Large environments: each live fork needs the RAM its services use; prefer short
  forks, or a bigger remote Linux machine.

## Code layout

```
src/chronobox/   vm.py (Firecracker backend) · workspace.py (main, checkpoints, forks,
                 reports, lessons, guard) · commands.py · service.py · server.py (MCP)
                 images.py · install.py (`chronobox setup`, `doctor`) · cli.py (`chronobox build`,
                 `images`, `init`, `guide`, `warm`, `clean`, `up`, `cp`, `down`, `mcp`)
                 config.py (chronobox.toml) · project.py (bring a project up, warm start, diff,
                 export, sync) · handoff/ (`verify_script`: scripts, running, replay, report,
                 files, review, tools) · guide.py (AGENTS.md) · copying.py (`chronobox cp`)
vm/              tools/ (tools drive: init, guest agent) · images/default/ (the default image)
tests/           unit tests + real-VM tests (skipped without /dev/kvm)
examples/notes/  a web app + PostgreSQL project with its chronobox.toml
docs/            these pages, the logo and the demo GIF
```
