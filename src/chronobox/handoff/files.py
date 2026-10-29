"""The project's files in a hand-off (what is deployed on the replay, the patch for the user), and
saving or withdrawing a hand-off on the host."""

from __future__ import annotations

import asyncio
import io
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from chronobox.config import ProjectConfig
from chronobox.handoff.scripts import PATCH_FILE, REPORT_FILE, Scripts
from chronobox.project import (
    EXPORTS_DIR,
    ProjectBoot,
    _git_diff,
    _unpack_for_diff,
    local_edits,
    pack_project,
    project_diff,
)
from chronobox.workspace import Workspace

HANDOFF_DIR = EXPORTS_DIR / "handoff"
WITHDRAWN_DIR = EXPORTS_DIR / "handoff-withdrawn"
LOCAL = "local"  # code_from: the project folder on the host, as the agent edited it
EXECUTABLE = 0o755


@dataclass(frozen=True)
class Project:
    """The project side of a hand-off: what is deployed on the replay and diffed for the patch."""

    boot: ProjectBoot

    @property
    def config(self) -> ProjectConfig:
        """The project's config."""
        return self.boot.config

    async def archive(self, workspace: Workspace, code_from: str) -> tuple[bytes, set[str]]:
        """The project files of ``code_from`` and the names it deleted since the session started.

        Raises:
            WorkspaceError: If ``code_from`` is neither a machine of the workspace nor ``local``.
            VMError: If the files cannot be read from the machine.
        """
        if code_from == LOCAL:
            # Only what was edited locally: the fork's folder is the machine after `setup`, and the
            # untouched local copies of files setup changed must not overwrite them.
            copied = await asyncio.to_thread(self.boot.copied.read_bytes)
            current = await asyncio.to_thread(pack_project, self.config)
            archive, _, deleted = await asyncio.to_thread(local_edits, copied, current)
            return archive, set(deleted)
        else:
            machine = workspace.machine(code_from)
            archive = await machine.get(self.config.target, exclude=self.config.exclude)
            start = await asyncio.to_thread(self.boot.baseline.read_bytes)
        return archive, file_names(start) - file_names(archive)

    async def patch(self, workspace: Workspace, code_from: str) -> str:
        """The patch the user applies to their copy of the project.

        Raises:
            ProjectError: If git fails on the host.
            VMError: If the files cannot be read from the machine.
        """
        if code_from != LOCAL:
            baseline = await asyncio.to_thread(self.boot.baseline.read_bytes)
            return await project_diff(workspace.machine(code_from), self.config, baseline)
        copied = await asyncio.to_thread(self.boot.copied.read_bytes)
        current = await asyncio.to_thread(pack_project, self.config)
        return await asyncio.to_thread(_archive_diff, copied, current)


def save_handoff(folder: Path, scripts: Scripts, patch: str, report: str) -> Path:
    """Write the hand-off (scripts, patch, report) to ``folder``, replacing what was there: the
    folder always holds the last verified hand-off."""
    _remove(folder)
    folder.mkdir(parents=True)
    for name, text in scripts.files().items():
        (folder / name).write_text(text)
        (folder / name).chmod(EXECUTABLE)
    if patch:
        (folder / PATCH_FILE).write_text(patch)
    (folder / REPORT_FILE).write_text(report + "\n")
    return folder


def withdraw_handoff(folder: Path, withdrawn: Path) -> bool:
    """Move a saved hand-off to ``withdrawn`` (replacing an older one); whether there was one."""
    if not folder.exists():
        return False
    _remove(withdrawn)
    folder.rename(withdrawn)
    return True


def single_file(name: str, content: bytes) -> bytes:
    """A tar archive holding one file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        member.mode = EXECUTABLE
        tar.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def file_names(archive: bytes) -> set[str]:
    """Paths of the files and links in a project archive, relative to the project folder."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {str(PurePosixPath(member.name)) for member in tar.getmembers() if not member.isdir()}


def _archive_diff(before: bytes, after: bytes) -> str:
    """The patch from one project archive to another, named by project paths.

    Raises:
        ProjectError: If git fails on the host.
    """
    with tempfile.TemporaryDirectory(prefix="chronobox-diff-") as work_dir:
        work = Path(work_dir)
        _unpack_for_diff(before, work / "host")
        _unpack_for_diff(after, work / "machine")
        return _git_diff(work)


def _remove(path: Path) -> None:
    """Delete a file, link or folder if it exists."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
