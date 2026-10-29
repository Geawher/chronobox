"""What the agent hands over: the scripts, how to replay them, and how the user runs them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

SCRIPT_FILE = "apply.sh"
PRE_FILE = "pre.sh"
ROLLBACK_FILE = "rollback.sh"
ASSUME_FILE = "assume.sh"
PATCH_FILE = "changes.patch"
REPORT_FILE = "replay.txt"
STATE_DIR = "/root/.chronobox"
SHEBANG = "#!"
DEFAULT_SHEBANG = "#!/bin/sh"
SH_INTERPRETERS = frozenset({"sh", "dash", "ash"})
DEFAULT_SCRIPT_TIMEOUT_S = 900.0
MAX_SCRIPT_TIMEOUT_S = 3600.0


# Bash features that /bin/sh (dash, busybox) lacks or reads differently.
BASHISMS = {
    "[[ ... ]]": re.compile(r"\[\["),
    "{1..N} ranges": re.compile(r"\{-?\d+\.\.-?\d+\}"),
    "the function keyword": re.compile(r"^\s*function\s+\w+", re.MULTILINE),
    "source": re.compile(r"^\s*source\s", re.MULTILINE),
    "arrays": re.compile(r"\b\w+=\("),
    "here-strings (<<<)": re.compile(r"<<<"),
    "${var//...} or ${var:N}": re.compile(r"\$\{\w+(//|:\d|: +-\d)"),
    "&> redirections": re.compile(r"&>"),
    "declare / let": re.compile(r"^\s*(declare|let)\s", re.MULTILINE),
}


@dataclass(frozen=True)
class Scripts:
    """What the agent hands over.

    Attributes:
        apply: Run right after the project's new files are deployed.
        pre: Run before they are deployed, while the old code still runs (expand, then contract).
        rollback: Undoes ``apply`` once the old files are back; replayed too.
        assume: Checks what the others rely on (sizes, versions, which node leads...); run first,
            and the user stops if it fails.
    """

    apply: str
    pre: str = ""
    rollback: str = ""
    assume: str = ""

    def files(self) -> dict[str, str]:
        """The non-empty scripts by file name, each with a ``#!`` line (``/bin/sh`` if it had none)."""
        named = {
            ASSUME_FILE: self.assume, PRE_FILE: self.pre,
            SCRIPT_FILE: self.apply, ROLLBACK_FILE: self.rollback,
        }  # fmt: skip
        return {name: runnable(text) for name, text in named.items() if text.strip()}


@dataclass(frozen=True)
class Options:
    """How to replay a hand-off (see ``verify_script``).

    Attributes:
        check: Must succeed after the script: the change works as the user asked.
        load: Traffic the agent runs during the replay, next to the project's own.
        load_check: Run once the traffic stopped; fails if users had a problem.
        sample: Commands that shrink the replay's data before anything else.
        twice: Also run the script again once it ended, and after a kill midway.
        timeout: Limit for each run of a script, in seconds.
    """

    check: str
    load: str = ""
    load_check: str = ""
    sample: str = ""
    twice: bool = True
    timeout: float = DEFAULT_SCRIPT_TIMEOUT_S


def runnable(script: str) -> str:
    """The script as saved and run: with its ``#!`` line, or ``#!/bin/sh`` added."""
    text = script.lstrip("\n")
    if not text.startswith(SHEBANG):
        text = f"{DEFAULT_SHEBANG}\n{text}"
    return text if text.endswith("\n") else text + "\n"


def bash_features(script: str) -> list[str]:
    """The bash features a script uses although its ``#!`` line runs it with sh."""
    first = runnable(script).splitlines()[0].removeprefix(SHEBANG).split()
    interpreter = first[-1] if first and first[0].endswith("env") else (first[0] if first else "")
    if PurePosixPath(interpreter).name not in SH_INTERPRETERS:
        return []
    return [feature for feature, pattern in BASHISMS.items() if pattern.search(script)]


def how_to_run(scripts: Scripts, has_patch: bool, folder: str) -> str:
    """What the user does with the hand-off, in order."""
    steps = []
    if scripts.assume.strip():
        steps.append(f"./{ASSUME_FILE} (it checks what the change relies on: stop if it fails)")
    if scripts.pre.strip():
        steps.append(f"./{PRE_FILE} (before the new files, while the old code still runs)")
    if has_patch:
        steps.append(f"apply {PATCH_FILE} to the project (git apply)")
    steps.append(f"./{SCRIPT_FILE}")
    undo = f" To undo: put the old files back, then ./{ROLLBACK_FILE}." if scripts.rollback.strip() else ""
    return f"to run on the real machine, as root, from {folder}: " + ", then ".join(steps) + "." + undo
