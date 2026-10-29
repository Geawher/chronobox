#!/bin/sh
# Record the README's demo as a real terminal session: interactive Claude Code on examples/notes
# while users keep writing notes. render.py turns the recording into demo.gif.
#
#   docs/demo/record.sh [folder]     (default: ~/Desktop/notes-demo, must not exist)
#
# Claude Code starts with the prompt already sent; when it is done, type /exit to stop recording.
# Set CHRONOBOX_VM_DIR to use VM files other than `chronobox setup`'s, and CLAUDE_CODE_OAUTH_TOKEN
# (from `claude setup-token`) to record with another account than the one you are logged in with:
# Claude Code shows the account's plan in its header.
set -e
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
work=${1:-$HOME/Desktop/notes-demo}
if [ -e "$work" ]; then
  echo "$work exists: remove it or pass another folder" >&2
  exit 1
fi
mkdir -p "$work"
cp -r "$repo/examples/notes/." "$work/"
cat >> "$work/chronobox.toml" <<'TOML'

# For the demo: users writing a note every 0.1 s, the whole session long.
[[service]]
name = "users"
start = "python3 traffic.py"
ready = "test -s /tmp/traffic-saved"
after_setup = true
TOML
env=""
[ -n "$CHRONOBOX_VM_DIR" ] && env=", \"env\": {\"CHRONOBOX_VM_DIR\": \"$CHRONOBOX_VM_DIR\"}"
cat > "$work/mcp.json" <<JSON
{"mcpServers": {"chronobox": {"command": "uv", "args": ["run", "--project", "$repo", "chronobox", "mcp", "--project", "$work"]$env}}}
JSON
# Warm the machine first, so the recording does not open on a minute of booting.
uv run --project "$repo" chronobox warm --project "$work"
cd "$work"
prompt=$(cat "$here/prompt.txt")
# No user settings (plugins, hooks, CLAUDE.md): what anyone gets with a fresh install; --verbose
# shows each tool call instead of folding them. Pauses longer than 2 s (the model thinking) are
# cut to 2 s.
asciinema rec "$here/demo.cast" --overwrite --idle-time-limit 2 --cols 110 --rows 32 \
  --title "Claude Code with chronobox" \
  --command "claude --model ${MODEL:-claude-sonnet-5-5} --setting-sources '' --verbose \
    --mcp-config mcp.json --strict-mcp-config \
    --allowedTools mcp__chronobox Read Edit Write Glob Grep -- '$prompt'"
echo "recorded $here/demo.cast; render it with (--start/--end in seconds cut the recording):"
echo "  uv run --with pillow --with pyte --with fonttools python $here/render.py $here/demo.cast $repo/docs/demo.gif --speed 1.5"
