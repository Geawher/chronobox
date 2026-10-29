"""What a replay recorded, and how it reads for the agent and in ``replay.txt``."""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field

from chronobox.handoff.scripts import ASSUME_FILE, SCRIPT_FILE

OUTPUT_LIMIT = 4000
STEP_OUTPUT_LIMIT = 1500
SHOWN_DIFFERENCES = 6


# Steps whose passing the summary at the top of the report lists.
SHOWN_CHECKS = ("check `", "[handoff] check `", "load_check `", "[handoff] load_check `")


LOAD_CHECK_LABEL = "load_check `"
SAMPLE_CAVEAT = "the logic was checked, not the time or locks at full size"


@dataclass(frozen=True)
class Step:
    """One command of a replay and how it ended."""

    label: str
    code: int
    output: str
    seconds: float


@dataclass
class Replay:
    """What happened when a hand-off was replayed on a fork.

    Attributes:
        fork: The fork it ran on; it stays open for inspection until the next replay starts.
        checkpoint: The checkpoint (or fork) it started from.
        code_from: Where the deployed project files came from (a machine, or ``local``).
        steps: Every command run, in order.
        problems: Why the hand-off cannot be trusted; empty when it passed.
        notes: What the user should know although it passed.
        sampled: The data was shrunk first (``sample``).
        current: The step running now, for a replay in the background.
        started: When the replay started (``time.monotonic``).
    """

    fork: str
    checkpoint: str
    code_from: str
    steps: list[Step] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sampled: bool = False
    current: str = "creating the fork"
    started: float = field(default_factory=time.monotonic)

    @property
    def succeeded(self) -> bool:
        """Every step and check passed."""
        return not self.problems

    def showed(self) -> list[str]:
        """What the steps of a passed replay showed, for whoever reviews the hand-off."""
        shown = [f"on a sample of the data ({SAMPLE_CAVEAT})"] if self.sampled else []
        for step in self.steps:
            label = step.label
            if label == ASSUME_FILE:
                shown.append(f"./{ASSUME_FILE} passed on the copy (the user runs it first on production)")
            elif label == SCRIPT_FILE:
                shown.append(f"./{SCRIPT_FILE} finished in {step.seconds:.1f} s")
            elif label == f"{SCRIPT_FILE} (second run)":
                shown.append(f"run again right after, it finished in {step.seconds:.1f} s")
            elif label.startswith("[killed") and label.endswith("(run again)"):
                shown.append(f"{label.split(']')[0].lstrip('[')}: it then finished the job")
            elif label.startswith("load `"):
                shown.append(f"traffic during the script: {label.removesuffix(' (started)')}")
            elif label.startswith(SHOWN_CHECKS):
                shown.append(f"passed: {label}")
        if not any(LOAD_CHECK_LABEL in step.label for step in self.steps):
            shown.append("not checked: what users saw while the script ran (no load_check)")
        return shown

    def report(self) -> str:
        """The replay as text, for the agent and for ``replay.txt``."""
        verdict = "passed" if self.succeeded else "FAILED"
        sample = f" ON A SAMPLE of the data ({SAMPLE_CAVEAT})"
        lines = [
            f"replayed on fork {self.fork!r} (from checkpoint {self.checkpoint!r}, project files "
            f"from {self.code_from}){sample if self.sampled else ''}: {verdict}"
        ]
        for step in self.steps:
            limit = OUTPUT_LIMIT if step.label.startswith(SCRIPT_FILE) else STEP_OUTPUT_LIMIT
            lines += [
                f"--- {step.label}: exit {step.code} after {step.seconds:.1f} s",
                shorten(step.output, limit),
            ]
        if self.succeeded:
            lines[1:1] = ["--- what this replay showed", *(f"- {line}" for line in self.showed())]
        if self.problems:
            lines += ["--- problems", *(f"- {problem}" for problem in self.problems)]
        if self.notes:
            lines += ["--- notes", *(f"- {note}" for note in self.notes)]
        return "\n".join(lines)


def difference(command: str, reference: str, expected: str, actual: str) -> str:
    """Why a ``compare`` command's outputs differ: what only each side has, and what to do."""
    only_reference, only_replay = [], []
    lines = difflib.unified_diff(expected.strip().splitlines(), actual.strip().splitlines(), n=0, lineterm="")
    for line in lines:
        if line.startswith(("---", "+++", "@@")):
            continue
        (only_reference if line.startswith("-") else only_replay).append(line[1:])
    parts = [f"`{command}` differs between {reference} and the replay"]
    if only_reference:
        parts.append(
            f"only {reference} has (the script does not make it: add it to the script, or if it is a "
            f"leftover of an experiment, remove it from {reference}):\n" + _listing(only_reference)
        )
    if only_replay:
        parts.append(
            f"only the replay has ({reference} lacks it: do that part of the change on {reference} too and "
            "test it there, or drop it from the script):\n" + _listing(only_replay)
        )
    return "\n".join(parts)


def _listing(lines: list[str]) -> str:
    """Up to ``SHOWN_DIFFERENCES`` lines, indented, with a count of the rest."""
    shown = [f"    {line}" for line in lines[:SHOWN_DIFFERENCES]]
    if len(lines) > SHOWN_DIFFERENCES:
        shown.append(f"    ... and {len(lines) - SHOWN_DIFFERENCES} more")
    return "\n".join(shown)


def shorten(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Long output keeps its start and end; no output reads ``(none)``."""
    text = text.strip()
    if len(text) <= limit:
        return text or "(none)"
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters cut ...]\n{text[-half:]}"
