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

We measured it on [chronobox-ops](https://github.com/Geawher/chronobox-ops), our benchmark of
operations tasks on live production machines. Each task has a hazard a careful engineer avoids:
traffic that must keep flowing, writes that must not be lost, a setting that only looks safe.
Every run is Claude Code with its normal tools; "alone" is the same Claude Code reaching the same
machine without chronobox's checkpoints, forks or hand-offs.

### Working on the live machine (34 tasks)

| Model | Runs per setup | Success: alone → chronobox | Harm: alone → chronobox | Cost per run | Median time |
|---|---|---|---|---|---|
| Haiku 4.5 | 102 | 29% → 37% | 55% → 43% | $0.15 → $0.19 | 2.2 → 3.4 min |
| Sonnet 5 | 102 | 75% → **85%** | 24% → **12%** | $0.46 → $0.56 | 4.3 → 6.1 min |
| Sonnet 5.5 | 204 | 72% → **86%** | 26% → **12%** | $0.11 → $0.13 | 1.6 → 1.9 min |
| Opus 4.8 | 204 / 102 | 82% → **89%** | 13% → **7%** | $1.09 → $1.26 | 5.0 → 7.5 min |
| Opus 5.5 | 102 | 96% → 96% | 3% → 4% | $0.30 → $0.34 | 3.2 → 4.5 min |

- **A cheaper model matches a stronger one.** Sonnet 5.5 with chronobox (86% success, 12% harm)
  does as well as Opus 4.8 alone (82%, 13%) at $0.13 per run instead of $1.09; the 95% intervals
  of the differences, [−3, +13] and [−7, +5] points, leave no gap we can measure.
- **The same model is safer.** Sonnet 5.5 succeeds on 14 points more runs and harms users half
  as often, both outside chance. Alone, its failures are production harm: lost writes, downtime.
  With forks it tries the risky step on a copy first and sees it break there.
- **What it costs:** about 15-25% more per run, and 20-50% more time, for the rehearsals.

### Handing over a script instead (10 tasks, preliminary)

On the 10 tasks that suit a hand-off (migrations, rolling restarts, cleanups), the agent can also
work on a copy and hand over a script, which `verify_script` replays under traffic, killed
midway and run again, before saving it. We graded each script on a production that had drifted
from the agent's copy (more rows, another leader), killed it halfway and ran it again:

| Model | Alone, live | chronobox, live | chronobox hand-off |
|---|---|---|---|
| Sonnet 5.5 | 17/30, harm 13, $0.15 | 20/30, harm 9, $0.19 | **24/30, harm 5**, $0.22 |
| Opus 4.8 | 18/30, harm 10, $1.64 | 22/30, harm 7, $1.89 | 18/30, harm 6, $2.12+ |
| Opus 5.5 | 28/30, harm 2, $0.42 | 28/30, harm 2, $0.44 | **30/30, harm 0**, $1.12 |

The hand-off was the safest way to work for every model, and Sonnet 5.5's hand-offs beat Opus
4.8 working live. Three seeds per cell, and the live columns ran a week earlier on older Claude
Code and chronobox versions: read these as directions, not final numbers.

### Where it does not help

- **The strongest model:** Opus 5.5 succeeds on 96% of live runs alone, often by building its own
  test copy, and gains nothing there.
- **The weakest:** Haiku 4.5 gains a little but still fails most hard tasks; a safe place to try
  a wrong idea does not make it right.
- **Slow, thorough models on a time limit:** 7 of Opus 4.8's 30 hand-offs ran out of the 30
  minutes before handing anything over, most of them still rehearsing by hand on forks.
- **Routine work:** on six simple changes to a small notes app (a column, a constraint, an
  index...), every model succeeded on 28-30 of 30 runs with no harm, with or without chronobox.

The benchmark is ours, and its tasks are built around hazards like these. Full results,
per-task tables and method:
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
with its own shell (bash if it has one, else `sh`; images without either are refused),
its `ENV` and its `WORKDIR`, as root (the image's `USER` is not used). Nothing is
started automatically; start the image's services from `--setup`, from a project's
`chronobox.toml` (below), or let the agent do it (e.g. `docker-entrypoint.sh postgres &`
in the postgres image).

Rebuilding an image never changes the disk under a running or saved machine: each machine
and snapshot keeps a hard link to the disk it was made on (a symlink if the VMs live on
another filesystem than the VM files).

### Your project

A `chronobox.toml` next to your code says how to bring it up; the agent then works on an
exact, running copy of it ([example](examples/notes/chronobox.toml): a web app and its
PostgreSQL database). `chronobox init` writes a commented starter file: it takes your
`Dockerfile` as the image if there is one and excludes the usual heavy folders; you add the
services and setup:

```toml
image = { dockerfile = "Dockerfile.dev" }   # or "name" of a built image, or { reference = "postgres:16" }
prepare = ["make build"]                    # after the code is copied, before services
setup = ["bin/migrate", "bin/seed"]         # once every service is ready

[machine]
mem_mib = 2048
disk_mib = 8192

[project]
exclude = ["node_modules", "*.log"]         # .git and .chronobox are never copied

[[service]]
name = "db"
start = "docker-entrypoint.sh postgres"     # runs in the foreground; restarted if it exits
ready = "pg_isready -h 127.0.0.1"
env = { POSTGRES_HOST_AUTH_METHOD = "trust" }
```

```bash
cd path/to/app
chronobox init                     # a starter chronobox.toml (never overwrites one) and AGENTS.md guide
chronobox build --project .        # the image it declares, once
chronobox warm                     # optional: prepare the warm start now, not in the first session
claude mcp add chronobox -- chronobox mcp --project .
```

(`chronobox` here is the installed command; from a checkout, use
`uv run --project /path/to/chronobox chronobox`.)

- The project is copied to `/src` (`[project] target`), where commands start.
- Services run under a small restart loop, logs in `/var/log/chronobox/<name>.log`. They start
  before `setup`; one that needs it (an app server that needs the migrations `setup` runs)
  says `after_setup = true`.
- **Warm start**: the first session prepares the machine and saves it
  (`~/.cache/chronobox/warm`); later sessions restore it in about 0.1 s, as long as the
  config, the project's files and the image are unchanged (`--no-warm` to boot fresh). The
  saved machine is also the session's `start` checkpoint.
- Three more tools:
  - `diff(where)`: what changed in the project on main or a fork since the machine was
    ready, as a patch for your local copy (`git apply` takes it; your own edits since then,
    and files the project's setup wrote, stay out of it);
  - `export(path, where)`: copy files out to `.chronobox/exports/`;
  - `sync(where)`: copy local edits of the project's files (new, changed, deleted since the
    session started) into main or a fork, for an agent that edits files locally but needs the
    machine to run them.
- **Hand-off scripts**: some changes are actions, not file edits (a migration, a backfill,
  restarting services, a setting outside the project). The agent writes them as a script and
  calls `verify_script(script, check)`, which replays it the way you will run it (`./apply.sh`:
  its `#!` line picks the shell, `/bin/sh` without one): on a new fork of the `start` checkpoint
  (the machine before the agent's work), after deploying the project's changed files (from main,
  a fork, or with `code_from="local"` the files edited locally). The replay passes only if:
  - the script succeeds, and again when run a second time, quickly (a retry must not redo the
    whole job), and when killed midway and run again, on copies of the replay snapshotted while
    it ran (deployments get retried, sometimes after a crash);
  - the agent's `check` passes (it is required), and the project's checks;
  - the traffic during the replay (the project's, the agent's own `load`, or the machine's own
    clients) lost nothing (`load_check`);
  - every service still answers its `ready` command, and a project process the agent restarted
    on its machine was restarted by the script too.

  Optional scripts are replayed in their place and handed over: `assume` (what the change relies
  on, run first: you stop if it fails), `pre` (before the new files, with the old code running)
  and `rollback` (once the old files are back). `sample` shrinks the replay's data so a long
  operation's logic is checked in minutes (the report says it ran on a sample), and
  `background=true` returns at once, the result coming from `verify_result` (a replay still going
  after 5 minutes goes on in the background the same way). Then the patch, the scripts and the
  replay's report, which starts with what the replay showed, are saved in `.chronobox/exports/handoff/`, ready for the real
  machine; a failed replay withdraws the hand-off saved before (to `handoff-withdrawn/`).
- **`[handoff]`**: what you know about production, added to every replay so the agent's own check
  is not the only one:

  ```toml
  [handoff]
  enabled = true                        # false: no verify_script, agents only change the machine
  prepare = ["bin/more-data"]           # make the replay look more like production first
  load = "bin/traffic"                  # users' traffic, running during the replay
  load_check = "bin/traffic --report"   # after it: fails if users lost writes
  checks = ["bin/smoke-test"]           # must pass after the script
  compare = ["pg_dump -s -U postgres"]  # same output on the agent's machine and on the replay
  sample = ["bin/keep-a-tenth"]         # optional: every replay on a sample of the data
  review = "claude -p --model claude-sonnet-5-5"  # optional: a second opinion on the agent's check
  ```

  `review` runs on your machine after each passed replay, with the user's goal, the scripts, the
  check and the report on its stdin. It answers `OK`, or `GAP` and up to two ways the change is
  likely to be wrong while the check passes, and the agent sees them. It only advises: the hand-off
  is saved either way. Use a strong model: on 274 verified hand-offs, Haiku flagged almost every
  one, good or bad, while Sonnet flagged 73% of those that later failed and 25% of those that
  worked. It catches checks that miss the obvious, not subtle traps: on the hand-offs of the
  hardest tasks it flagged failures and successes alike.

  Agents are told the user's request decides how they deliver: asked to make a change, they make
  it on the machine (rehearsing on a fork); asked for a script, they hand it over with
  `verify_script`; unsure, they ask. A project that never hands changes over sets
  `enabled = false` (or the server runs with `--no-handoff`): no `verify_script`, and no hand-off
  text in the agent's instructions or the guide.

  `compare` catches a script that does not reproduce what the agent tested (a column it added
  by hand, a setting it forgot); the [example](examples/notes/chronobox.toml) compares the schema
  and the API's fields, with [traffic.py](examples/notes/traffic.py) as its users.
- **A guide for agents**: `chronobox init` (or `chronobox guide`, to refresh it) writes how to work
  with the machine into the project's `AGENTS.md`, between marker lines, keeping the rest of the
  file. Codex, Cursor and others read AGENTS.md; Claude Code reads it when the project has no
  `CLAUDE.md` (otherwise add a line `@AGENTS.md` to CLAUDE.md, which `chronobox guide` reminds you).
- Machines have no network: install everything in the image.

To work on the same machine as the agent, keep it running yourself:

```bash
chronobox up                                    # in the project; Ctrl-C or `chronobox down` stops it
chronobox cp ./fixtures main:/src/fixtures      # in: a folder's content, or a file, into a folder
chronobox cp main:/src/report.csv ./out         # out (a fork works too: try-1:/src/...)
```

While `chronobox up` runs, `chronobox mcp --project .` (so every agent session) works on that
machine instead of starting its own, and keeps it when the session ends.

Options: `--project DIR`, `--no-warm`, `--image NAME` (default `default`),
`--profile alone|rollback|chronobox` (default `chronobox`), `--guard`, `--no-handoff`,
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
| `diff(where)` / `export(path, where)` / `sync(where)` | all, with `--project` | the project's changes as a patch / copy files out / copy local edits in |
| `verify_script(script, check, ...)` / `verify_result(fork, wait)` | chronobox, with `--project` or `--handoff` | replay a change script on a fresh fork (retries, kills, traffic, the project's checks), then save the hand-off / collect a replay run in the background |

With `--profile chronobox`, risky commands (deletes, kills, restarts, SQL changes,
overwrites) are checkpointed automatically first.

The tool descriptions steer the agent to the safe pattern on a machine that serves
users: fork, try the change and let time pass on the fork, then apply the verified
change to the main machine. They also say when not to: a rehearsal takes as long as the real
thing, so when users are failing now the agent first takes a small step it knows is safe to stop
the damage, and rehearses only what it is unsure of. A rollback is exact, so it also undoes the writes users
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
                 images.py · cli.py (`chronobox build`, `images`, `init`, `guide`, `warm`, `up`, `cp`,
                 `down`, `mcp`) · config.py (chronobox.toml) · project.py (bring a project up, warm
                 start, diff, export, sync) · handoff.py (`verify_script`) · guide.py (AGENTS.md)
                 · copying.py (`chronobox cp`)
vm/              tools/ (tools drive: init, guest agent) · images/default/ · fetch.sh · build_rootfs.sh
tests/           unit tests + real-VM tests (skipped without /dev/kvm)
examples/notes/  a web app + PostgreSQL project with its chronobox.toml
```

## License

Apache-2.0, see [LICENSE](LICENSE).
