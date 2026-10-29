# Security

- **The machine is isolated.** Each one is a Firecracker microVM with no network interface: what
  runs in it cannot reach your host, your network or the internet. The project is copied in, not
  mounted; files come out only through `diff`, `export` and hand-offs, into `.chronobox/`.
- **Commands inside run as root, by design.** Treat the machine as the agent's, not as a sandbox
  for untrusted code you then trust: a rollback restores it, nothing more.
- **`chronobox.toml` is code, like a Makefile.** Image builds run the project's Dockerfile with
  your Docker, and `[handoff] review` runs its command on your host. Use chronobox on repositories
  you trust.
- **`--http` serves the machine to whoever has the URL.** Bind it to an address only your agent
  can reach, and keep the token secret (it is random unless you set one).
- **The rehearsal guard (`--guard`) is a safety net for mistakes**, a heuristic over command text,
  not a security boundary.

Report a vulnerability privately: see [SECURITY.md](../SECURITY.md).
