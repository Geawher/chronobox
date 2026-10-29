"""Script hand-off: check a change script by replaying it on a fresh fork, then save it for the user.

Some changes are actions, not file edits: a migration or a backfill, restarting services in order,
a setting outside the project. A patch cannot carry them, so the agent writes them as a shell
script. ``verify_script`` replays that script the way the user will run it on the real machine
(``./apply.sh``: by its ``#!`` line, ``/bin/sh`` when it has none): on a new fork of a checkpoint
(by default ``start``, the machine as it was before the agent's work), after deploying the
project's changed files there (from main, a fork, or the local folder).

A replay passes only when all of these hold:

- ``assume`` (what the script relies on) holds on the replay, as it must on the real machine;
- the script succeeds; with ``twice`` (the default) it also succeeds when run again once it ended,
  quickly (a retry must not redo the whole job), and when killed midway and run again, on copies
  of the replay taken while it ran: deployments get retried, sometimes after a crash;
- the agent's own ``check`` succeeds, then the project's ``[handoff]`` checks;
- every service still answers its ``ready`` command;
- the traffic run during the replay (the project's ``[handoff] load`` and the agent's ``load``)
  saw no problem (``load_check``);
- the ``[handoff] compare`` commands print the same on the agent's machine and on the replay;
- a project process the agent restarted on its machine was restarted by the script too, so the
  real machine does not keep running the old code.

``sample`` commands may shrink the replay's data first, so a long operation's logic is checked in
minutes; the report then says so. Then the project patch, the scripts and the replay's report are
saved together in ``.chronobox/exports/handoff/``. A failed replay withdraws the hand-off saved
before (moved to ``handoff-withdrawn``): it no longer matches what the agent is working on.

Scripts run detached in their own session and are polled: a snapshot resets the machine's
connections, so nothing may be waiting on one while the replay is snapshotted for the crash test.
A replay can also run in the background, collected later with ``verify_result``.

Modules: ``scripts`` (what the agent hands over), ``running`` (a detached script on a machine),
``replay`` (one replay and its checks), ``report`` (what it recorded), ``files`` (the project's files,
saving a hand-off), ``review`` (the optional second opinion) and ``tools`` (the agent's tools).
"""

from __future__ import annotations

from chronobox.handoff.files import HANDOFF_DIR, LOCAL, WITHDRAWN_DIR, Project, save_handoff, withdraw_handoff
from chronobox.handoff.replay import replay_script
from chronobox.handoff.report import Replay, Step, difference
from chronobox.handoff.review import review
from chronobox.handoff.running import CHECK_TIMEOUT_S, POLL_TIMEOUT_S, TIMED_OUT, kill_script, run_detached
from chronobox.handoff.scripts import (
    ASSUME_FILE,
    PATCH_FILE,
    PRE_FILE,
    REPORT_FILE,
    ROLLBACK_FILE,
    SCRIPT_FILE,
    STATE_DIR,
    Options,
    Scripts,
    bash_features,
    how_to_run,
    runnable,
)
from chronobox.handoff.tools import handoff_tools

__all__ = [
    "ASSUME_FILE",
    "CHECK_TIMEOUT_S",
    "HANDOFF_DIR",
    "LOCAL",
    "Options",
    "PATCH_FILE",
    "POLL_TIMEOUT_S",
    "PRE_FILE",
    "Project",
    "REPORT_FILE",
    "ROLLBACK_FILE",
    "Replay",
    "SCRIPT_FILE",
    "STATE_DIR",
    "Scripts",
    "Step",
    "TIMED_OUT",
    "WITHDRAWN_DIR",
    "bash_features",
    "difference",
    "handoff_tools",
    "how_to_run",
    "kill_script",
    "replay_script",
    "review",
    "run_detached",
    "runnable",
    "save_handoff",
    "withdraw_handoff",
]
