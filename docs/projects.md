# Your own image and your project

By default each session gets a fresh machine running the default image. This page shows how to run
your own system instead: any Docker image, or your project with its services and data, described in
a `chronobox.toml`.

## Your own image

Any Docker image can be the machine's system, from a registry or from your Dockerfile:

```bash
chronobox build --image postgres:16                      # -> image "postgres-16"
chronobox build --dockerfile ./Dockerfile --name myapp   # context: the Dockerfile's folder
chronobox images
claude mcp add chronobox -- chronobox mcp --image myapp
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

## Your project

A `chronobox.toml` next to your code says how to bring it up; the agent then works on an
exact, running copy of it ([example](../examples/notes/chronobox.toml): a web app and its
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
chronobox init                     # a starter chronobox.toml (never overwrites one), AGENTS.md guide,
                                   # and .chronobox/ in .gitignore
chronobox build --project .        # the image it declares, once
chronobox warm                     # optional: prepare the warm start now, not in the first session
claude mcp add chronobox -- chronobox mcp --project .
```

- The project is copied to `/src` (`[project] target`), where commands start.
- Services run under a small restart loop, logs in `/var/log/chronobox/<name>.log`. They start
  before `setup`; one that needs it (an app server that needs the migrations `setup` runs)
  says `after_setup = true`.
- Machines have no network: install everything in the image.

### Warm starts

The first session prepares the machine and saves it (`~/.cache/chronobox/warm`); later sessions
restore it in about 0.1 s, as long as the config, the project's files and the image are unchanged
(`--no-warm` to boot fresh). The saved machine is also the session's `start` checkpoint.

A project keeps one warm start (a new one replaces the old); `chronobox clean` deletes those of
projects that are gone (moved, deleted, a removed worktree) or unused for 30 days
(`--unused-days N`, `--all`, `--dry-run` to list them with their size first). Saving a warm
start also deletes those of gone projects. "Gone" means its `chronobox.toml` cannot be found at
that moment, which also happens on a branch without one or on an unmounted drive: that project
then boots fresh once and saves a new warm start.

### Getting changes out

With `--project`, the agent has three more tools:

- `diff(where)`: what changed in the project on main or a fork since the machine was
  ready, as a patch for your local copy (`git apply` takes it; your own edits since then,
  and files the project's setup wrote, stay out of it);
- `export(path, where)`: copy files out to `.chronobox/exports/`;
- `sync(where)`: copy local edits of the project's files (new, changed, deleted since the
  session started) into main or a fork, for an agent that edits files locally but needs the
  machine to run them.

Changes that are actions rather than file edits (a migration, a restart) are handed over as a
script: see [Hand-offs](handoff.md).

### A guide for agents

`chronobox init` (or `chronobox guide`, to refresh it) writes how to work with the machine into the
project's `AGENTS.md`, between marker lines, keeping the rest of the file. Codex, Cursor and others
read AGENTS.md; Claude Code reads it when the project has no `CLAUDE.md` (otherwise add a line
`@AGENTS.md` to CLAUDE.md, which `chronobox guide` reminds you).

## Working on the same machine as the agent

To work on the same machine as the agent, keep it running yourself:

```bash
chronobox up                                    # in the project; Ctrl-C or `chronobox down` stops it
chronobox cp ./fixtures main:/src/fixtures      # in: a folder's content, or a file, into a folder
chronobox cp main:/src/report.csv ./out         # out (a fork works too: try-1:/src/...)
```

While `chronobox up` runs, `chronobox mcp --project .` (so every agent session) works on that
machine instead of starting its own, and keeps it when the session ends.

## Options of `chronobox mcp`

All of them: `chronobox mcp --help`.

| Option | What it does |
|---|---|
| `--project DIR` | run the project described by `DIR/chronobox.toml` |
| `--no-warm` | boot fresh instead of restoring the warm start |
| `--image NAME` | the machine's image (default `default`) |
| `--profile alone\|rollback\|chronobox` | which [tools](tools.md) the agent gets (default `chronobox`) |
| `--guard` | refuse destructive commands on main until they succeeded on a fork |
| `--no-handoff` | no `verify_script`, and no hand-off text for the agent |
| `--handoff-dir DIR` | hand-offs without a project |
| `--setup script.sh` | prepare the machine at boot: install, start services, load data |
| `--mem`, `--disk` | the machine's memory and writable disk, in MiB (default 1024 and 2048) |
| `--command-timeout` | seconds per command, default 300 |
| `--root DIR` | where the session's VMs and checkpoints go |
