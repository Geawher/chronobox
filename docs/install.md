# Installing chronobox

The [README](../README.md#install) has the three commands most people need. This page has the rest:
other MCP clients, where chronobox keeps its files, uninstalling, and working from a checkout.

## Requirements

- **x86_64 Linux with KVM.** macOS and Windows are not supported (Firecracker needs KVM); a Linux
  VM with nested virtualization, or a remote Linux machine, works.
- **Docker**, to build machine images.
- [uv](https://docs.astral.sh/uv/). No root.

```bash
uv tool install git+https://github.com/Geawher/chronobox
chronobox setup     # Firecracker, a guest kernel, the tools drive and the default image (5-10 min, once)
chronobox doctor    # checks KVM, Docker and the files, then boots a machine
```

If `doctor` says KVM or Docker is not usable by your user:

```bash
sudo usermod -aG kvm,docker $USER   # then log out and back in
```

## Connect your agent

chronobox is an MCP server. In Claude Code, from the folder you work in:

```bash
claude mcp add chronobox -- chronobox mcp                     # a fresh machine for each session
claude mcp add chronobox -- chronobox mcp --project .         # your project running on it
claude mcp add -s user chronobox -- chronobox mcp             # a fresh machine, in every folder
```

Each session then gets its own machine (boot ≈ 1 s). Any MCP client works the same way: the
command is `chronobox` with the arguments `mcp` (and `--project /path/to/app`, see
[Your project](projects.md)). For example in Codex (`~/.codex/config.toml`) or Cursor
(`.cursor/mcp.json`):

```toml
[mcp_servers.chronobox]
command = "chronobox"
args = ["mcp", "--project", "/path/to/app"]
```

```json
{ "mcpServers": { "chronobox": { "command": "chronobox", "args": ["mcp", "--project", "/path/to/app"] } } }
```

## Disk use, and uninstalling

| What | Where | Size |
|---|---|---|
| VM files: Firecracker, kernel, tools drive, images | `~/.local/share/chronobox/vm` (`CHRONOBOX_VM_DIR`) | about 700 MB, plus each image you build |
| Warm starts: one per project | `~/.cache/chronobox/warm` | 1-3 GB each |
| Running sessions' machines and checkpoints | `~/.cache/chronobox/sessions` | deleted when the session ends |

`chronobox clean` deletes the warm starts of projects that are gone or unused for 30 days, and
what killed or crashed sessions left behind (`--dry-run` lists it all first). To remove
everything:

```bash
uv tool uninstall chronobox
rm -rf ~/.local/share/chronobox ~/.cache/chronobox
docker image rm chronobox-tools $(docker image ls -q 'chronobox-image-*')
```

## From a checkout

```bash
git clone https://github.com/Geawher/chronobox && cd chronobox
uv sync
uv run chronobox setup        # the same VM files as an installed chronobox
uv run pytest                 # includes real-VM tests (CHRONOBOX_REQUIRE_VM=1: fail instead of skip)
claude mcp add chronobox -- uv run --project "$PWD" chronobox mcp
```
