# The README's demo

`demo.gif` is a real Claude Code session, replayed: every tool call and message in order, with
long commands and outputs cut to their first lines.

```bash
CLAUDE_CODE_OAUTH_TOKEN=... docs/demo/record.sh                  # session.jsonl (Sonnet 5.5 by default)
uv run --with pillow python docs/demo/render.py docs/demo/session.jsonl docs/demo.gif
```

`record.sh` copies [examples/notes](../../examples/notes), adds a service of users writing a note
every 0.1 s, and runs Claude Code on [prompt.txt](prompt.txt) with only chronobox's tools and file
edits. `session.jsonl` keeps the model, Claude Code's version and the messages.
