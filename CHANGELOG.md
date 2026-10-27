# Changelog

## 0.2.0 (2026-10)

### Install
- `chronobox setup` downloads Firecracker and a kernel and builds the tools drive and the default
  image, so `uv tool install git+https://github.com/Geawher/chronobox` is enough. It replaces
  `vm/fetch.sh` and `vm/build_rootfs.sh`. ([#15])
- `chronobox doctor` checks KVM, Docker and the VM files, then boots a machine. ([#15])
- `--version` on `chronobox` and `chronobox-mcp`.

### Your own systems
- Any Docker image or Dockerfile as the machine's system: `chronobox build --image` /
  `--dockerfile`, `chronobox images`. ([#6])
- Projects: a `chronobox.toml` brings your code, services and data up on the machine, with warm
  starts (about 0.1 s), `diff`, `export` and `sync`; `chronobox init`, `warm`, `up`, `cp`, `down`,
  and an `AGENTS.md` guide. ([#7], [#8], [#9])
- `chronobox clean` deletes warm starts of gone or unused projects, and what killed sessions left
  behind. ([#13], [#15])

### Hand-offs
- `verify_script` replays a change script on a fresh fork of the starting machine, under the
  project's traffic, run twice and killed midway, with the agent's and the project's checks, then
  saves the patch and scripts as a hand-off. `[handoff]` adds what you know about production:
  `load`, `checks`, `compare`, `sample`, and an optional `review` command. ([#10], [#11])
- The user's request decides between changing the machine and handing over a script;
  `[handoff] enabled = false` or `--no-handoff` turns hand-offs off. ([#12])

### Agents
- Tool descriptions steer to fork, try, then apply, and warn that a rollback also loses the
  users' writes. ([#4])

### Changed
- A session's VMs and checkpoints live under `~/.cache/chronobox/sessions`, never in the folder
  the server starts from. ([#15])
- `--http-token` is random by default (it was `mcp`). ([#15])
- `chronobox init` adds `.chronobox/` to `.gitignore`. ([#15])

### Removed
- `vm/fetch.sh` and `vm/build_rootfs.sh`: use `chronobox setup`. ([#15])

## 0.1.0 (2026-10)

First public release: undo and forks of live Linux machines for coding agents, as an MCP server
(Firecracker microVMs). Checkpoint, rollback, fork, compare, promote and discard, with reports,
lessons and an optional rehearsal guard. ([#1], [#2], [#3])

[#1]: https://github.com/Geawher/chronobox/pull/1
[#2]: https://github.com/Geawher/chronobox/pull/2
[#3]: https://github.com/Geawher/chronobox/pull/3
[#4]: https://github.com/Geawher/chronobox/pull/4
[#6]: https://github.com/Geawher/chronobox/pull/6
[#7]: https://github.com/Geawher/chronobox/pull/7
[#8]: https://github.com/Geawher/chronobox/pull/8
[#9]: https://github.com/Geawher/chronobox/pull/9
[#10]: https://github.com/Geawher/chronobox/pull/10
[#11]: https://github.com/Geawher/chronobox/pull/11
[#12]: https://github.com/Geawher/chronobox/pull/12
[#13]: https://github.com/Geawher/chronobox/pull/13
[#15]: https://github.com/Geawher/chronobox/pull/15
