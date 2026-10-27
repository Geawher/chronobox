#!/bin/sh
# Record the README's demo: a real Claude Code session on examples/notes while users keep writing
# notes, saved as session.jsonl (Claude Code's stream-json). render.py turns it into demo.gif.
#
#   CLAUDE_CODE_OAUTH_TOKEN=... docs/demo/record.sh [model]
set -e
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
work=$(mktemp -d)
cp -r "$repo/examples/notes/." "$work/"
cat >> "$work/chronobox.toml" <<'TOML'

# For the demo: users writing a note every 0.1 s, the whole session long.
[[service]]
name = "users"
start = "python3 traffic.py"
ready = "test -s /tmp/traffic-saved"
after_setup = true
TOML
cat > "$work/mcp.json" <<JSON
{"mcpServers": {"chronobox": {"command": "uv", "args": ["run", "--project", "$repo", "chronobox", "mcp", "--project", "$work"]}}}
JSON
cd "$work"
claude -p "$(cat "$here/prompt.txt")" \
  --model "${1:-claude-sonnet-5-5}" \
  --mcp-config mcp.json --strict-mcp-config \
  --allowedTools "mcp__chronobox" Read Edit Write Glob Grep \
  --output-format stream-json --verbose > "$work/raw.jsonl"
# Keep what the demo shows: the model and version, then the messages (the full init event also
# lists this machine's own plugins, skills and paths).
python3 - "$work/raw.jsonl" > "$here/session.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    event = json.loads(line)
    if event.get("type") == "system" and event.get("subtype") == "init":
        event = {key: event[key] for key in ("type", "subtype", "model", "claude_code_version")}
    elif event.get("type") not in ("assistant", "user", "result"):
        continue
    elif event["type"] == "assistant":
        parts = event["message"]["content"]
        event["message"]["content"] = [part for part in parts if part.get("type") != "thinking"]
    print(json.dumps(event))
PY
echo "recorded $here/session.jsonl (project copy: $work)"
