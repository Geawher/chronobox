# The agent's tools

What the agent sees through MCP. `--profile` picks the set (default `chronobox`); `alone` and
`rollback` are mostly for comparisons like the benchmark's.

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
| `diff(where)` / `export(path, where)` / `sync(where)` | all, with `--project` | the project's changes as a patch / copy files out / copy local edits in ([more](projects.md#getting-changes-out)) |
| `verify_script(script, check, ...)` / `verify_result(fork, wait)` | chronobox, with `--project` or `--handoff-dir` | replay a change script on a fresh fork (retries, kills, traffic, the project's checks), then save the hand-off / collect a replay run in the background ([more](handoff.md)) |

With `--profile chronobox`, risky commands (deletes, kills, restarts, SQL changes,
overwrites) are checkpointed automatically first.

## How the tools steer the agent

The tool descriptions steer the agent to the safe pattern on a machine that serves
users: fork, try the change and let time pass on the fork, then apply the verified
change to the main machine. They also say when not to: a rehearsal takes as long as the real
thing, so when users are failing now the agent first takes a small step it knows is safe to stop
the damage, and rehearses only what it is unsure of. A rollback is exact, so it also undoes the
writes users made since the checkpoint; on a machine that takes writes, fixing forward is usually
better.

The optional **rehearsal guard** (`--guard`) refuses destructive commands on the main machine until
they succeeded on a fork (or are forced). It is a heuristic over command text, a safety net for
mistakes, not a security boundary.

There is no search or autopilot inside: the agent decides when to use the tools.
