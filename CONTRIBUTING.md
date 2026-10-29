# Contributing

Issues and pull requests are welcome. For a larger change, open an issue first so we can agree
on the approach.

## Set up

You need x86_64 Linux with KVM, Docker, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Geawher/chronobox && cd chronobox
uv sync
uv run chronobox setup     # the VM files, once
uv run chronobox doctor    # checks this machine can run them
```

## Before a pull request

```bash
uv run ruff check src tests vm
uv run ruff format src tests vm
CHRONOBOX_REQUIRE_VM=1 uv run pytest    # real-VM tests included; without the variable they skip
```

CI has no KVM, so it skips the real-VM tests: run them locally, and say in the pull request that
you did.

## Style

- Every function, test and helper has a docstring: one line when that says it, Google style
  (`Args:`, `Returns:`, `Raises:`) when it needs more.
- Name constants instead of writing numbers inline, and keep comments for the why.
- Errors a user can hit are one line that says what to do (`chronobox: ...`), not a traceback.
- One topic per pull request; describe what changed in each file and why.

## Reporting a vulnerability

Not in a public issue: see [SECURITY.md](SECURITY.md).

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
