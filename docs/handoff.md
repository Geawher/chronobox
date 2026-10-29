# Hand-offs: a script replayed before you run it

Some changes are actions, not file edits: a migration, a backfill, restarting services, a setting
outside the project. Instead of making them on the machine, the agent can write them as a script
that you run on the real system. chronobox checks that script first, the way you will run it.
Hand-offs need a [project](projects.md) (or `--handoff-dir`).

## `verify_script`

The agent calls `verify_script(script, check)`, which replays the script the way you will run it
(`./apply.sh`: its `#!` line picks the shell, `/bin/sh` without one): on a new fork of the `start`
checkpoint (the machine before the agent's work), after deploying the project's changed files (from
main, a fork, or with `code_from="local"` the files edited locally). The replay passes only if:

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
after 5 minutes goes on in the background the same way).

Then the patch, the scripts and the replay's report, which starts with what the replay showed, are
saved in `.chronobox/exports/handoff/`, ready for the real machine; a failed replay withdraws the
hand-off saved before (to `handoff-withdrawn/`).

## `[handoff]` in chronobox.toml

What you know about production, added to every replay so the agent's own check is not the only
one:

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

`compare` catches a script that does not reproduce what the agent tested (a column it added
by hand, a setting it forgot); the [example](../examples/notes/chronobox.toml) compares the schema
and the API's fields, with [traffic.py](../examples/notes/traffic.py) as its users.

## A second opinion: `review`

`review` runs on your machine after each passed replay, with the user's goal, the scripts, the
check and the report on its stdin. It answers `OK`, or `GAP` and up to two ways the change is
likely to be wrong while the check passes, and the agent sees them. It only advises: the hand-off
is saved either way.

Use a strong model: on 274 verified hand-offs, Haiku flagged almost every one, good or bad, while
Sonnet flagged 73% of those that later failed and 25% of those that worked. It catches checks that
miss the obvious, not subtle traps: on the hand-offs of the hardest tasks it flagged failures and
successes alike.

## When agents hand over, and when they don't

Agents are told the user's request decides how they deliver: asked to make a change, they make
it on the machine (rehearsing on a fork); asked for a script, they hand it over with
`verify_script`; unsure, they ask. A project that never hands changes over sets
`enabled = false` (or the server runs with `--no-handoff`): no `verify_script`, and no hand-off
text in the agent's instructions or the guide.

How hand-offs did in the benchmark: [Does chronobox help?](results.md#handing-over-a-script-instead-10-tasks-preliminary).
