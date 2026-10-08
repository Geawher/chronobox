"""Classify shell commands by how much harm they can do.

Three questions the workspace asks about a command:

- ``is_risky``: worth an automatic checkpoint first (deletes, kills, restarts,
  SQL changes, overwriting a file);
- ``needs_rehearsal``: destructive or hard to undo, so the guard wants to see it
  succeed on a fork before it runs on the main machine (plain file writes are
  checkpointed but not blocked: blocking them would stop all real work);
- ``changes_state``: anything that is not clearly read-only, for fork reports.

These are heuristics over the command text, not a sandbox: they catch the usual
spellings and miss creative ones.
"""

from __future__ import annotations

import re

_RISKY = re.compile(
    r"\b(rm|mv|truncate|dd|mkfs|shred|kill|pkill|killall|systemctl|service|pg_ctl|pg_ctlcluster"
    r"|dropdb|reboot|shutdown|chmod|chown|git\s+(reset|clean|checkout|push|rebase))\b"
    r"|\b(drop|delete|truncate|alter|update|vacuum\s+full|reindex|cluster)\b"
    r"|>\s*/(?!dev/null)",
    re.IGNORECASE,
)
_NEEDS_REHEARSAL = re.compile(
    r"\b(rm|mv|truncate|dd|mkfs|shred|kill|pkill|killall|reboot|shutdown|dropdb|pg_ctl"
    r"|pg_ctlcluster|migrate|upgrade)\b"
    r"|\b(systemctl|service)\s+\S*\s*(restart|stop|reload)\b|\bsystemctl\s+(restart|stop|reload)\b"
    r"|\b(drop|delete\s+from|truncate|alter|update\s+\w+\s+set|vacuum\s+full|reindex|cluster)\b"
    r"|\b(pip|pip3|uv\s+pip|apt|apt-get)\s+(install|uninstall|remove|upgrade)\b"
    r"|\buv\s+(add|sync|remove)\b"
    r"|\bgit\s+(reset|clean|checkout|push|rebase)\b",
    re.IGNORECASE,
)
_READ_ONLY = re.compile(
    r"^\s*(cat|ls|grep|egrep|ps|head|tail|less|more|wc|find|stat|df|du|free|env|echo|printf"
    r"|which|type|pwd|id|whoami|uname|date|sleep|file|diff|cmp|md5sum|sha256sum|ss|netstat"
    r"|lsof|pgrep|journalctl|ip|hostname|uptime|top|jq|sort|uniq|cut|awk|tree|realpath"
    r"|strings|hexdump|od|xxd|readelf|objdump|base64|nl|column|test|\[|true"
    r"|systemctl\s+status|git\s+(status|log|diff|show)|curl\s+-s[^|]*$|nginx\s+-t"
    r"|sqlite3\s+\S+\s+[\"']?\s*(select|pragma|\.schema|\.tables))\b",
    re.IGNORECASE,
)
_WRITES = re.compile(r">|\bsed\s+-i|\btee\b|-X\s*(POST|PUT|DELETE|PATCH)", re.IGNORECASE)
_HARMLESS_REDIRECTS = re.compile(r"\d?>\s*/dev/null|\d?>&\d|&>\s*/dev/null")


def is_risky(command: str) -> bool:
    return bool(_RISKY.search(command))


def needs_rehearsal(command: str) -> bool:
    return bool(_NEEDS_REHEARSAL.search(command))


def changes_state(command: str) -> bool:
    command = _HARMLESS_REDIRECTS.sub("", command)
    return not _READ_ONLY.search(command) or bool(_WRITES.search(command))


def normalize(command: str) -> str:
    """The command with whitespace collapsed, so rehearsals match regardless of spacing."""
    return " ".join(command.split())
