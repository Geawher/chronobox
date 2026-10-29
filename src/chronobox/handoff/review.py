"""The optional second opinion (``[handoff] review``): a command on the host that reads a passed
hand-off and says what its check may miss."""

from __future__ import annotations

import asyncio

from chronobox.handoff.report import OUTPUT_LIMIT, STEP_OUTPUT_LIMIT, shorten
from chronobox.handoff.scripts import Options, Scripts

# A reviewer (``[handoff] review``) gets this long to answer; its first line is OK when it sees no gap.
# The prompt asks for likely gaps only: in an offline test on 274 verified hand-offs, asking for
# any gap made every reviewer flag nearly every hand-off, which told agents nothing.
REVIEW_TIMEOUT_S = 300.0


REVIEW_OK = "OK"


REVIEW_PROMPT = """You review a change script before it runs on a production machine. \
It passed a replay on a copy of the machine, judged by the check below. Your job: find ways \
the change could still be wrong on production while that check passes.

What the user asked:
{goal}

The scripts (run as ./apply.sh on production; assume.sh first, if any):
{scripts}

The check that has to prove the change works:
{check}

Traffic during the replay: {load}
Traffic check after it: {load_check}

The replay's report:
{report}

Judge it like a careful senior engineer who must decide whether to run this on production now. \
Report a gap only if you think it is more likely than not that, on production, the change does not \
do what the user asked or harms users while this check passes. A risk the replay already rules \
out, or one that is merely possible, is not a gap. Consider in particular: writes by users while \
the script runs; rows that arrive after it; data the logic gets wrong (records that look alike but \
are not the same, nulls, other owners); effects that come later (scheduled jobs, compaction, \
caches); production being bigger than the copy.

The first line of your answer is exactly OK or exactly GAP. After GAP, give at most 2 gaps, most \
likely first, each in one sentence with a shell command the check could add to catch it."""


async def review(command: str, goal: str, scripts: Scripts, options: Options, report: str) -> str:
    """Ask the project's reviewer (a command on the host, the prompt on its stdin) what the check
    misses; its answer, as a report section, with what to do about it.

    The reviewer only advises: the hand-off is saved either way, and a reviewer that fails or does
    not answer in time is reported as such.
    """
    prompt = REVIEW_PROMPT.format(
        goal=goal.strip() or "(not given: judge from the scripts and the check)",
        scripts="\n".join(f"--- {name}\n{text}" for name, text in scripts.files().items()),
        check=options.check, load=options.load or "(none of the agent's)",
        load_check=options.load_check or "(none)", report=report,
    )  # fmt: skip
    header = f"--- review (by `{command}`)"
    try:
        process = await asyncio.create_subprocess_shell(
            command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        output, _ = await asyncio.wait_for(process.communicate(prompt.encode()), REVIEW_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        return f"{header}\nthe reviewer did not answer within {REVIEW_TIMEOUT_S:.0f} s"
    except OSError as error:
        return f"{header}\nthe reviewer could not run: {error}"
    answer = output.decode(errors="replace").strip()
    if process.returncode != 0:
        failed = shorten(answer, STEP_OUTPUT_LIMIT)
        return f"{header}\nthe reviewer failed (exit {process.returncode}): {failed}"
    if answer.splitlines()[:1] == [REVIEW_OK]:
        return f"{header}\n{REVIEW_OK}: no gap found in the check"
    return (
        f"{header}\n{shorten(answer, OUTPUT_LIMIT)}\nThe hand-off is saved, but the reviewer thinks the "
        "check may miss the above: if a gap is real, make the check (or load_check) catch it, fix the "
        "script if needed, and verify again"
    )
