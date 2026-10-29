# The README's demo

`demo.gif` is a real terminal recording: interactive Claude Code (Sonnet 5.5) with chronobox on
[examples/notes](../../examples/notes) while users keep writing notes. Pauses longer than 2 s (the
model thinking) are cut to 2 s, and the GIF plays at 1.5x.

```bash
docs/demo/record.sh                       # demo.cast (asciinema); type /exit once Claude Code is done
uv run --with pillow --with pyte --with fonttools \
  python docs/demo/render.py docs/demo/demo.cast docs/demo.gif --start 1.2 --end 65.9 --speed 1.5
```

`record.sh` copies the example to `~/Desktop/notes-demo` (or the folder you pass), adds a service of
users writing a note every 0.1 s, prepares its warm start, then records Claude Code started with
[prompt.txt](prompt.txt), chronobox's tools, `--verbose` (each tool call shown) and no user
settings. It needs [asciinema](https://asciinema.org) and a real terminal window.

`render.py` replays the recording through a terminal emulator ([pyte](https://github.com/selectel/pyte))
and draws each screen with Pillow, so making the GIF needs no other program. `--start` and `--end`
(seconds into the recording) cut it to the session.
