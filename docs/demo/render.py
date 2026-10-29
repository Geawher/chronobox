"""Render a recorded Claude Code session (record.sh's session.jsonl) as the README's demo GIF.

Every tool call and agent message of the session is shown in order, shortened to fit: long
commands and outputs keep their first lines. Reading files is collapsed into one line.

    uv run --with pillow python docs/demo/render.py docs/demo/session.jsonl docs/demo.gif
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLUMNS = 92
ROWS = 24
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_SIZE = 15
CELL_W, CELL_H = 9, 20
PAD = 18
BAR = 30
COLORS = {
    "bg": (15, 23, 38),
    "bar": (23, 35, 56),
    "text": (232, 237, 245),
    "dim": (139, 150, 170),
    "call": (242, 169, 59),
    "fork": (43, 179, 163),
    "ok": (120, 200, 140),
    "user": (170, 190, 255),
}
TYPE_FRAME_MS = 40
CHARS_PER_FRAME = 6
COMMAND_LINES = 4
RESULT_LINES = 3
HIDDEN_TOOLS = {"ToolSearch", "Glob"}
SESSION_TOOL = "mcp__chronobox__"


@dataclass
class Line:
    """One terminal row: its text, colour, and whether it is typed out or appears at once."""

    text: str
    color: str = "text"
    typed: bool = False
    bold: bool = False


def wrap(text: str, indent: str = "  ", limit: int | None = None) -> list[str]:
    """Wrap ``text`` to the terminal, ``indent`` before its first row and spaces before the others;
    keep the first ``limit`` rows."""
    rows: list[str] = []
    for raw in text.splitlines() or [""]:
        rows += textwrap.wrap(raw, COLUMNS - len(indent), replace_whitespace=False) or [""]
    if limit is not None and len(rows) > limit:
        rows = rows[:limit] + ["…"]
    return [(indent if index == 0 else " " * len(indent)) + row for index, row in enumerate(rows)]


def result_text(content: object) -> str:
    """The text of a tool result, without the JSON wrapper chronobox's tools reply in."""
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = str(content)
    with contextlib.suppress(ValueError, KeyError, TypeError):
        text = json.loads(text)["result"]
    return re.sub(r"\n\[exit code 0\]$", "", text.strip())


def call_lines(name: str, arguments: dict) -> list[Line]:
    """The lines announcing one tool call."""
    if name.startswith(SESSION_TOOL):
        tool = name.removeprefix(SESSION_TOOL)
        where = arguments.get("where") or arguments.get("name") or ""
        head = f"● chronobox.{tool}" + (f" [{where}]" if where else "")
        color = "fork" if tool in {"fork", "discard", "promote"} or where not in {"", "main"} else "call"
        body = arguments.get("command") or arguments.get("lesson") or ""
        prefix = "  $ " if "command" in arguments else "  "
        rows = wrap(body, prefix, COMMAND_LINES) if body else []
        return [Line(head, color, typed=True, bold=True)] + [Line(row, "text", typed=True) for row in rows]
    target = arguments.get("file_path", "")
    return [Line(f"● {name} {Path(target).name}", "call", typed=True, bold=True)]


def session_lines(path: Path) -> list[Line]:
    """Every line of the demo, in the order the session produced them."""
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    lines: list[Line] = []
    reads: list[str] = []
    calls: dict[str, str] = {}
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            prompt = (path.parent / "prompt.txt").read_text().strip()
            lines += [Line("$ claude", "dim")] + [Line(row, "user", typed=True) for row in wrap(prompt, "> ")]
            lines.append(Line(""))
        for part in event.get("message", {}).get("content", []) if event.get("type") == "assistant" else []:
            if part["type"] == "text" and event is not events[-2]:
                first = re.split(r"(?<=[.:])\s", part["text"].strip(), maxsplit=1)[0]
                lines += [Line(row, "dim", typed=True) for row in wrap(first, "  ")]
            elif part["type"] == "tool_use":
                calls[part["id"]] = part["name"]
                if part["name"] in HIDDEN_TOOLS:
                    continue
                if part["name"] == "Read":
                    reads.append(Path(part["input"]["file_path"]).name)
                    continue
                if reads:
                    lines.append(Line("● Read " + ", ".join(reads), "call", typed=True, bold=True))
                    reads = []
                lines += call_lines(part["name"], part["input"])
        for part in event.get("message", {}).get("content", []) if event.get("type") == "user" else []:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            if not calls.get(part["tool_use_id"], "").startswith(SESSION_TOOL):
                continue
            text = result_text(part["content"])
            for row in wrap(text, "  → ", RESULT_LINES):
                color = "ok" if "checkpoint" in row or "copy of the machine" in row else "dim"
                lines.append(Line(row, color))
        if event.get("type") == "result":
            lines.append(Line(""))
            summary = event["result"].strip().split("\n\n")[0].replace("`", "")
            lines += [Line(row, "text", bold=True) for row in wrap("✓ " + summary, "")]
    return lines


def draw(lines: list[Line], fonts: tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]) -> Image.Image:
    """One frame: the last ``ROWS`` lines in a terminal window."""
    width, height = PAD * 2 + COLUMNS * CELL_W, BAR + PAD * 2 + ROWS * CELL_H
    frame = Image.new("RGB", (width, height), COLORS["bg"])
    pen = ImageDraw.Draw(frame)
    pen.rectangle((0, 0, width, BAR), fill=COLORS["bar"])
    for index, color in enumerate(((232, 128, 90), (242, 169, 59), (43, 179, 163))):
        pen.ellipse((14 + index * 20, 10, 24 + index * 20, 20), fill=color)
    pen.text((width // 2, BAR // 2), "claude · chronobox", fill=COLORS["dim"], font=fonts[0], anchor="mm")
    for row, line in enumerate(lines[-ROWS:]):
        font = fonts[1] if line.bold else fonts[0]
        pen.text((PAD, BAR + PAD + row * CELL_H), line.text[:COLUMNS], fill=COLORS[line.color], font=font)
    return frame


def render(session: Path, target: Path) -> None:
    """Type the session out line by line and save the frames as a looping GIF."""
    fonts = (ImageFont.truetype(FONT, FONT_SIZE), ImageFont.truetype(FONT_BOLD, FONT_SIZE))
    frames: list[Image.Image] = []
    durations: list[int] = []
    shown: list[Line] = []
    for line in session_lines(session):
        if line.typed and len(line.text) > CHARS_PER_FRAME:
            for end in range(CHARS_PER_FRAME, len(line.text), CHARS_PER_FRAME * 3):
                frames.append(draw([*shown, Line(line.text[:end], line.color, bold=line.bold)], fonts))
                durations.append(TYPE_FRAME_MS)
        shown.append(line)
        frames.append(draw(shown, fonts))
        durations.append(900 if line.text.startswith("●") else 160 if line.typed else 300)
    durations[-1] = 8000
    palette = frames[-1].quantize(colors=32)
    frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    size_kib = target.stat().st_size // 1024
    print(f"{target}: {len(frames)} frames, {sum(durations) / 1000:.0f} s, {size_kib} KiB")


if __name__ == "__main__":
    render(Path(sys.argv[1]), Path(sys.argv[2]))
