# Security

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub: on the repository's **Security** tab,
choose **Report a vulnerability**. Do not open a public issue for them.

You will get an answer within a week. Fixes go into the latest release only.

## What chronobox protects, and what it does not

chronobox gives coding agents a Linux machine they can checkpoint, roll back and fork. Its
boundaries:

- **In scope:** escaping the machine. Each machine is a Firecracker microVM with no network
  interface; anything in it reaching the host, the host's network or files outside what chronobox
  copies out (`diff`, `export`, hand-offs into `.chronobox/`) is a vulnerability.
- **In scope:** the MCP server over `--http` serving tool calls without the URL's token, or the
  `chronobox up` socket being reachable by other users.
- **Not a boundary:** commands inside the machine run as root by design. The rehearsal guard
  (`--guard`) is a heuristic over command text that catches mistakes; bypassing it is not a
  vulnerability.
- **Trusted input:** a project's `chronobox.toml` and Dockerfile are code you run, like a
  Makefile. Image builds run with your Docker, and `[handoff] review` runs its command on your
  host.
