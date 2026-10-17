"""The chronobox section of a project's ``AGENTS.md``: how agents should work with the machine.

``AGENTS.md`` is read by most coding agents (Codex, Cursor, Claude Code when the project has no
``CLAUDE.md``...), so the guide reaches whichever agent uses the MCP server. The MCP server's own
instructions say the same in short; the guide adds the workflow around the tools.

The section sits between two marker lines, so ``chronobox guide`` can rewrite it without touching
the rest of the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chronobox.config import ProjectConfig

AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
START_MARKER = "<!-- chronobox:start (written by `chronobox guide`; edits here are replaced) -->"
END_MARKER = "<!-- chronobox:end -->"
CLAUDE_IMPORT = "@AGENTS.md"
GUIDE = """\
## Working with chronobox

This project runs in a chronobox machine: a Linux VM with the project copied at `{target}` and its
services ({services}) running, restarted if they exit (logs in `/var/log/chronobox/`). Agents use it
through the `chronobox` MCP tools.

### Where things run
- Run the project, its tests and its services only in the machine (`run`): they are not set up here.
- Edit files here as usual, then `sync` to copy your edits into the machine (main or a fork) before
  running them there. If you edit in the machine instead, `diff` gives the change back as a patch.
- `export` copies files out of the machine into `.chronobox/exports/`.

### Trying things safely (when you have `fork`)
- Before a change whose effect you are not sure of, `fork` and try it there (`run` and `wait` with
  `where=<fork>`); check that it works and that no data was lost; then do it on main and `discard`
  the fork.
- A rehearsal takes as long as the real thing. Rehearse what you are unsure of, not what you know;
  for a long operation, try it on a sample or try only its risky steps.
- When users are failing now, or will be before a full rehearsal could end (an outage, a limit
  about to run out), first take a small step on main that you know is safe and that stops or delays
  the damage. Then rehearse the real fix.
- `checkpoint` and `rollback` restore main exactly, but a rollback also loses what users wrote
  since the checkpoint: on a machine that takes writes, prefer fixing forward.

### Handing a change over (when you have `verify_script`)
- A change that needs more than file edits (a migration, a backfill, a restart, a setting outside
  the project) is handed over as a script plus the patch. The user runs `./apply.sh` on the real
  machine right after applying the patch: its `#!` line picks the shell (`/bin/sh` without one).
- `verify_script(script, check)` replays the script on a fresh copy of the machine as it was before
  your work, with your files deployed. It runs it again once it ended (that second run must be
  quick), kills it midway on copies and runs it again there, then runs your check and the
  project's own checks. When everything passes, `.chronobox/exports/handoff/` holds the hand-off; a
  failed replay withdraws the one saved before. Pass `code_from="local"` when you edited the files
  here.
- Your `check` decides whether you are done: make it fail unless the change works the way the user
  asked, used as users would (call the new endpoint, insert a new row and read it back), not only
  that the services are up.
- The check runs after the script and cannot see what users went through during it. When the
  change could lose or block writes (restarts, failovers, migrations under traffic), pass `load`
  (writes like the users', each logged when acknowledged) and `load_check` (fails if an
  acknowledged write is missing).
- `assume` holds what the script relies on (table sizes, versions, which node leads): the user runs
  it first and stops if it fails, so a production that differs from your copy is caught before
  anything changes.
- Write the script for the real machine, which has more data and live users: no values you only
  saw in this session, batches instead of one long locking statement, skip what is already done
  (a retry must finish the job, also after a crash halfway), and wait for what you restart to
  answer before moving on.
- Mind the time: a replay takes as long as the script, plus the retries, and the call waits for
  it. Know roughly how long the script takes before replaying it in full. For a long operation,
  pass `sample` (commands that shrink the data) to check the logic in minutes, or
  `background=true` and collect the result later with `verify_result`. If users are failing now,
  take the safe step on main first and hand the full change over after.
"""


@dataclass(frozen=True)
class GuideResult:
    """What ``write_guide`` did.

    Attributes:
        file: The ``AGENTS.md`` written.
        created: The file did not exist before.
        claude_hint: The project has a ``CLAUDE.md`` that does not import ``AGENTS.md``, so Claude
            Code will not read the guide unless it is imported.
    """

    file: Path
    created: bool
    claude_hint: bool


def guide_section(config: ProjectConfig) -> str:
    """The guide for this project, between its markers."""
    services = ", ".join(service.name for service in config.services) or "none yet"
    return f"{START_MARKER}\n{GUIDE.format(target=config.target, services=services)}{END_MARKER}\n"


def write_guide(config: ProjectConfig) -> GuideResult:
    """Add the guide to the project's ``AGENTS.md``, or replace the guide already in it.

    The rest of the file is kept as it is. A project with a ``CLAUDE.md`` gets no edit there: the
    result says whether it should import ``AGENTS.md``.
    """
    file = config.path / AGENTS_FILE
    section = guide_section(config)
    created = not file.exists()
    text = "" if created else file.read_text()
    if START_MARKER in text and END_MARKER in text:
        before, rest = text.split(START_MARKER, 1)
        after = rest.split(END_MARKER, 1)[1].removeprefix("\n")
        text = f"{before}{section}{after}"
    else:
        text = f"{text.rstrip()}\n\n{section}" if text.strip() else section
    file.write_text(text)
    claude = config.path / CLAUDE_FILE
    hint = claude.is_file() and CLAUDE_IMPORT not in claude.read_text()
    return GuideResult(file, created, hint)
