"""``chronobox cp``: where each side of a copy is, and the tar archives that carry it.

A side is a local path, or ``WHERE:/PATH`` on a running project's machine, where WHERE is
``main`` or a fork's name. Both directions follow one rule: a folder's content is copied into
the destination folder, a file goes into it under its own name.
"""

from __future__ import annotations

import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

REMOTE = re.compile(r"^(?P<where>[A-Za-z0-9][\w.-]*):(?P<path>/.*)$")


class CopyError(Exception):
    """A copy that cannot be done as asked."""


@dataclass(frozen=True)
class Location:
    path: str
    where: str | None = None  # None: on this computer

    @property
    def is_remote(self) -> bool:
        return self.where is not None


def parse_location(text: str) -> Location:
    """``main:/src/app.py`` or ``fork-1:/src`` is on a machine; anything else is local
    (write ``./a:/b`` for a local path that looks remote)."""
    match = REMOTE.match(text)
    if match is None:
        return Location(text)
    return Location(match["path"], match["where"])


def check_direction(source: Location, destination: Location) -> None:
    """Check that one side is on the machine and the other here.

    Raises:
        CopyError: If both or neither are on the machine.
    """
    if source.is_remote == destination.is_remote:
        raise CopyError("one side must be on the machine (main:/path or FORK:/path) and the other here")


def pack_local(path: Path) -> bytes:
    """A tar archive of a local folder's content, or of one file under its own name.

    Raises:
        CopyError: If ``path`` does not exist.
    """
    if not path.exists():
        raise CopyError(f"{path} does not exist")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(path, arcname="." if path.is_dir() else path.name)
    return buffer.getvalue()


def unpack_local(archive: bytes, destination: Path) -> list[str]:
    """Unpack into ``destination`` (created if missing); returns the top-level names.

    The ``data`` filter refuses members that would land outside ``destination`` (absolute
    paths, ``..``, links pointing out), since the archive comes from the machine.

    Raises:
        CopyError: If ``destination`` is a file or the archive holds such a member.
    """
    if destination.is_file():
        raise CopyError(f"{destination} is a file; the destination must be a folder")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(destination, filter="data")
        except tarfile.FilterError as error:
            raise CopyError(f"refused to unpack {error}") from error
        names = {Path(member.name).parts[0] for member in tar.getmembers() if member.name != "."}
    return sorted(names)
