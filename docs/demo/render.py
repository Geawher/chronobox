"""Render an asciinema recording (record.sh's demo.cast) as the README's demo GIF.

The recording is replayed through a terminal emulator (pyte), sampled 10 times a second; each
screen that changed becomes a frame, held as long as it was on screen (the recording already cut
pauses to 2 s).

    uv run --with pillow --with pyte --with fonttools \
      python docs/demo/render.py docs/demo/demo.cast docs/demo.gif [--start S] [--end S] [--speed X]
"""

from __future__ import annotations

import argparse
import json
import re
from functools import cache
from pathlib import Path

import pyte
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

FONTS = "/usr/share/fonts/truetype"
FONT = f"{FONTS}/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = f"{FONTS}/dejavu/DejaVuSansMono-Bold.ttf"
# Glyphs DejaVu Sans Mono lacks (Claude Code's ⏺, ✻, ⎿...) come from these, in order.
FALLBACKS = (f"{FONTS}/noto/NotoSansSymbols2-Regular.ttf", f"{FONTS}/noto/NotoSansSymbols-Regular.ttf",
             f"{FONTS}/noto/NotoSansMath-Regular.ttf")  # fmt: skip
FONT_SIZE = 15
CELL_W, CELL_H = 9, 19
PAD = 14
BACKGROUND = (15, 23, 38)
FOREGROUND = (232, 237, 245)
# The 16 named colours, as pyte names them.
NAMED = {
    "black": (40, 44, 52), "red": (232, 128, 90), "green": (120, 200, 140), "brown": (242, 169, 59),
    "yellow": (242, 169, 59), "blue": (110, 150, 240), "magenta": (200, 140, 230), "cyan": (43, 179, 163),
    "white": (220, 225, 235), "brightblack": (139, 150, 170), "brightred": (240, 150, 120),
    "brightgreen": (150, 220, 160), "brightbrown": (250, 200, 110), "brightyellow": (250, 200, 110),
    "brightblue": (150, 180, 250), "brightmagenta": (220, 170, 240), "brightcyan": (90, 210, 195),
    "brightwhite": (250, 250, 252),
}  # fmt: skip
FRAME_S = 0.1  # the screen is sampled 10 times a second; unchanged samples extend the frame before
LAST_FRAME_MS = 6000
INFINITY = float("inf")
# Terminal graphics commands (Claude Code's logo in terminals that draw images): pyte prints them.
GRAPHICS_START = "\x1b_"
GRAPHICS = re.compile(r"\x1b_.*?\x1b\\", re.DOTALL)


def color(name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """An RGB colour from pyte's name for it: a named colour, a 6-digit hex value, or the default."""
    if name in NAMED:
        return NAMED[name]
    if len(name) == 6:
        try:
            return int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16)
        except ValueError:
            pass
    return default


@cache
def characters(path: str) -> frozenset[int]:
    """The code points a font file has glyphs for."""
    return frozenset(TTFont(path).getBestCmap())


@cache
def font_for(char: str, bold: bool) -> ImageFont.FreeTypeFont:
    """DejaVu Sans Mono (bold or not) if it has ``char``, else the first fallback that has it."""
    path = next((path for path in (FONT, *FALLBACKS) if ord(char) in characters(path)), FONT)
    return ImageFont.truetype(FONT_BOLD if bold and path == FONT else path, FONT_SIZE)


def draw(screen: pyte.Screen) -> Image.Image:
    """The screen as an image."""
    image = Image.new("RGB", (PAD * 2 + screen.columns * CELL_W, PAD * 2 + screen.lines * CELL_H), BACKGROUND)
    pen = ImageDraw.Draw(image)
    for row in range(screen.lines):
        line = screen.buffer[row]
        for column in range(screen.columns):
            cell = line[column]
            fg, bg = color(cell.fg, FOREGROUND), color(cell.bg, BACKGROUND)
            if cell.reverse:
                fg, bg = bg, fg
            x, y = PAD + column * CELL_W, PAD + row * CELL_H
            if bg != BACKGROUND:
                pen.rectangle((x, y, x + CELL_W - 1, y + CELL_H - 1), fill=bg)
            if cell.data.strip():
                pen.text((x, y + 1), cell.data, fill=fg, font=font_for(cell.data, cell.bold))
    return image


def render(
    cast: Path, target: Path, start: float = 0.0, end: float = float("inf"), speed: float = 1.0
) -> None:
    """Replay ``cast`` and save each screen it showed between ``start`` and ``end`` (seconds into the
    recording) as a frame of a looping GIF, played ``speed`` times faster."""
    lines = cast.read_text().splitlines()
    header = json.loads(lines[0])
    screen = pyte.Screen(header["width"], header["height"])
    stream = pyte.Stream(screen)
    screen.dirty.clear()
    frames: list[Image.Image] = []
    starts: list[float] = []
    tick = 0.0
    pending = ""
    for line in lines[1:]:
        at, kind, data = json.loads(line)
        if kind != "o":
            continue
        while tick <= min(at, end):  # the screen at each tick, when it changed since the last frame
            shows_something = frames or any(row.strip() for row in screen.display)  # no blank opening
            if start <= tick and shows_something and (screen.dirty or not frames):
                frames.append(draw(screen))
                starts.append(tick)
                screen.dirty.clear()
            tick += FRAME_S
        if at > end:
            break
        data, pending = without_graphics(pending + data)
        stream.feed(data)
    else:
        frames.append(draw(screen))
        starts.append(tick)
    ends = [*starts[1:], starts[-1] + LAST_FRAME_MS / 1000 * speed]
    durations = [round((stop - begin) * 1000 / speed) for begin, stop in zip(starts, ends, strict=True)]
    durations[-1] = LAST_FRAME_MS
    palette = frames[-1].quantize(colors=64)
    frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    size_kib = target.stat().st_size // 1024
    print(f"{target}: {len(frames)} frames, {sum(durations) / 1000:.0f} s, {size_kib} KiB")


def without_graphics(data: str) -> tuple[str, str]:
    """``data`` without terminal graphics commands (``ESC _ ... ESC \\``, which pyte would print as
    text), and an unfinished one at its end, kept for the next chunk."""
    data = GRAPHICS.sub("", data)
    unfinished = data.rfind(GRAPHICS_START)
    if unfinished == -1:
        return data, ""
    return data[:unfinished], data[unfinished:]


def main() -> None:
    """Parse the command line and render."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cast", type=Path)
    parser.add_argument("gif", type=Path)
    parser.add_argument("--start", type=float, default=0.0, help="seconds into the recording to start at")
    parser.add_argument(
        "--end", type=float, default=float("inf"), help="seconds into the recording to end at"
    )
    parser.add_argument("--speed", type=float, default=1.0, help="play this many times faster")
    args = parser.parse_args()
    render(args.cast, args.gif, args.start, args.end, args.speed)


if __name__ == "__main__":
    main()
