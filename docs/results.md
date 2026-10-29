# Does chronobox help?

The [README](../README.md#does-it-help) has the headline. This page has both measurements and
where chronobox does not help.

We measured it on [chronobox-ops](https://github.com/Geawher/chronobox-ops), our benchmark of
operations tasks on live production machines. Each task has a hazard a careful engineer avoids:
traffic that must keep flowing, writes that must not be lost, a setting that only looks safe.
Every run is Claude Code with its normal tools; "alone" is the same Claude Code reaching the same
machine without chronobox's checkpoints, forks or hand-offs.

## Working on the live machine (34 tasks)

| Model | Runs per setup | Success: alone → chronobox | Harm: alone → chronobox | Cost per run | Median time |
|---|---|---|---|---|---|
| Haiku 4.5 | 102 | 29% → 37% | 55% → 43% | $0.15 → $0.19 | 2.2 → 3.4 min |
| Sonnet 5 | 102 | 75% → **85%** | 24% → **12%** | $0.46 → $0.56 | 4.3 → 6.1 min |
| Sonnet 5.5 | 204 | 72% → **86%** | 26% → **12%** | $0.11 → $0.13 | 1.6 → 1.9 min |
| Opus 4.8 | 204 / 102 | 82% → **89%** | 13% → **7%** | $1.09 → $1.26 | 5.0 → 7.5 min |
| Opus 5.5 | 102 | 96% → 96% | 3% → 4% | $0.30 → $0.34 | 3.2 → 4.5 min |

- **A cheaper model matches a stronger one.** Sonnet 5.5 with chronobox (86% success, 12% harm)
  does as well as Opus 4.8 alone (82%, 13%) at $0.13 per run instead of $1.09; the 95% intervals
  of the differences, [−3, +13] and [−7, +5] points, leave no gap we can measure.
- **The same model is safer.** Sonnet 5.5 succeeds on 14 points more runs and harms users half
  as often, both outside chance. Alone, its failures are production harm: lost writes, downtime.
  With forks it tries the risky step on a copy first and sees it break there.
- **What it costs:** about 15-25% more per run, and 20-50% more time, for the rehearsals.

## Handing over a script instead (10 tasks, preliminary)

On the 10 tasks that suit a hand-off (migrations, rolling restarts, cleanups), the agent can also
work on a copy and hand over a script, which `verify_script` replays under traffic, killed
midway and run again, before saving it ([hand-offs](handoff.md)). We graded each script on a
production that had drifted from the agent's copy (more rows, another leader), killed it halfway
and ran it again:

| Model | Alone, live | chronobox, live | chronobox hand-off |
|---|---|---|---|
| Sonnet 5.5 | 17/30, harm 13, $0.15 | 20/30, harm 9, $0.19+ | **24/30, harm 5**, $0.22+ |
| Opus 4.8 | 18/30, harm 10, $1.64+ | 22/30, harm 7, $1.89+ | 18/30, harm 6, $2.12+ |
| Opus 5.5 | 28/30, harm 2, $0.42 | 28/30, harm 2, $0.44 | **30/30, harm 0**, $1.12 |

The hand-off was the safest way to work for every model, and Sonnet 5.5's hand-offs beat Opus
4.8 working live. A "+" marks costs that leave out runs killed at the 30-minute limit, which
record none. Three seeds per cell, and the live columns ran a week earlier on older Claude
Code and chronobox versions: read these as directions, not final numbers.

## Where it does not help

- **The strongest model:** Opus 5.5 succeeds on 96% of live runs alone, often by building its own
  test copy, and gains nothing there.
- **The weakest:** Haiku 4.5 gains a little but still fails most hard tasks; a safe place to try
  a wrong idea does not make it right.
- **Slow, thorough models on a time limit:** 7 of Opus 4.8's 30 hand-offs ran out of the 30
  minutes before handing anything over, most of them still rehearsing by hand on forks.
- **Routine work:** on six simple changes to a small notes app (a column, a constraint, an
  index...), every model succeeded on 28-30 of 30 runs with no harm, with or without chronobox.

The benchmark is ours, and its tasks are built around hazards like these. Full results,
per-task tables and method:
[chronobox-ops results](https://github.com/Geawher/chronobox-ops/blob/main/results/README.md).
