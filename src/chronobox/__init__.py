"""chronobox: a Linux machine with exact undo and forks, for coding agents."""

from importlib.metadata import version

from chronobox.service import WorkspaceService, call_remote, request
from chronobox.vm import FirecrackerBackend, Machine, MachineSpec, Snapshot, VMError
from chronobox.workspace import Profile, Workspace, WorkspaceError

__all__ = [
    "FirecrackerBackend",
    "Machine",
    "MachineSpec",
    "Profile",
    "Snapshot",
    "VMError",
    "Workspace",
    "WorkspaceError",
    "WorkspaceService",
    "call_remote",
    "request",
]

__version__ = version("chronobox")
