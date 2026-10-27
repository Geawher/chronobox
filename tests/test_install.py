"""chronobox setup and doctor, cleaning crashed sessions, and .chronobox/ in .gitignore (no VM)."""

import fcntl
import io
import os
import tarfile
import time
from pathlib import Path

import pytest

from chronobox import install, server
from chronobox.cli import ignore_state, main
from chronobox.install import FIRECRACKER_VERSION, SetupError, check_kvm, check_vm_files, fetch_firecracker
from chronobox.vm import IN_USE_LOCK, UNLOCKED_SESSION_AGE_S, clean_sessions, freed_bytes


def _session(root: Path, name: str, *, locked: bool | None) -> Path:
    """A session folder with a 1 MiB disk; ``locked`` True/False gives it a held/free lock, None none."""
    folder = root / name
    (folder / "vms" / "vm0").mkdir(parents=True)
    (folder / "vms" / "vm0" / "data.ext4").write_bytes(b"x" * 2**20)
    if locked is not None:
        (folder / IN_USE_LOCK).touch()
    return folder


def test_cleaning_sessions_deletes_only_those_no_backend_holds(tmp_path: Path) -> None:
    """A held lock keeps a session; a free one, or an old folder with none, is deleted."""
    running = _session(tmp_path, "running", locked=True)
    crashed = _session(tmp_path, "crashed", locked=False)
    old = _session(tmp_path, "old", locked=None)
    young = _session(tmp_path, "young", locked=None)
    long_ago = time.time() - UNLOCKED_SESSION_AGE_S - 60
    for path in (old, *old.rglob("*")):
        os.utime(path, (long_ago, long_ago))
    with (running / IN_USE_LOCK).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        listed = clean_sessions(tmp_path, dry_run=True)
        assert {leftover.folder.name for leftover in listed} == {"crashed", "old"}
        assert crashed.exists() and old.exists()
        cleaned = clean_sessions(tmp_path)
    assert {leftover.folder.name for leftover in cleaned} == {"crashed", "old"}
    assert all(leftover.size_bytes >= 2**20 for leftover in cleaned)
    assert running.exists() and young.exists() and not crashed.exists() and not old.exists()


def test_cleaning_sessions_without_a_sessions_folder(tmp_path: Path) -> None:
    """No sessions folder yet: nothing to clean."""
    assert clean_sessions(tmp_path / "none") == []


def test_freed_bytes_leaves_out_files_linked_from_elsewhere(tmp_path: Path) -> None:
    """A disk hard-linked from the VM files frees nothing; two links inside count once."""
    shared = tmp_path / "rootfs.ext4"
    shared.write_bytes(b"x" * 2**20)
    session = tmp_path / "session"
    session.mkdir()
    os.link(shared, session / "image.ext4")
    (session / "data.ext4").write_bytes(b"y" * 2**20)
    os.link(session / "data.ext4", session / "data-copy.ext4")
    assert 2**20 <= freed_bytes(session) < 2 * 2**20


def test_init_ignores_the_state_folder_in_a_git_repository(tmp_path: Path) -> None:
    """Added once, after the existing lines; not in a folder that is no git repository."""
    assert not ignore_state(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("node_modules")
    assert ignore_state(tmp_path)
    assert not ignore_state(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "node_modules\n.chronobox/\n"


def test_init_keeps_a_gitignore_that_already_ignores_it(tmp_path: Path) -> None:
    """Any usual spelling of the folder counts."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("/.chronobox\n")
    assert not ignore_state(tmp_path)


def test_doctor_names_each_missing_file_and_the_fix(tmp_path: Path) -> None:
    """Every VM file is checked, and each missing one points to `chronobox setup`."""
    checks = check_vm_files(tmp_path)
    assert [check.name for check in checks] == ["Firecracker", "guest kernel", "tools drive", "default image"]
    assert all(not check.ok and check.fix == "chronobox setup" for check in checks)


def test_doctor_explains_kvm_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No /dev/kvm, or one this user cannot open, each come with what to do."""
    monkeypatch.setattr(install, "KVM", tmp_path / "kvm")
    assert "BIOS" in check_kvm().fix
    (tmp_path / "kvm").touch(mode=0o000)
    if os.access(tmp_path / "kvm", os.R_OK):
        pytest.skip("running as root")
    assert "usermod -aG kvm" in check_kvm().fix


def test_doctor_fails_with_exit_1_and_says_how(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing VM files: a FAIL line with the fix, exit status 1, and no boot attempt."""
    with pytest.raises(SystemExit) as stopped:
        main(["--vm-dir", str(tmp_path), "doctor"])
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL  Firecracker" in out and "fix: chronobox setup" in out and "a machine boots" not in out


def test_fetching_firecracker_keeps_only_the_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The release archive is unpacked into an executable ``firecracker``; the archive is removed."""
    member = f"release-{FIRECRACKER_VERSION}-x86_64/firecracker-{FIRECRACKER_VERSION}-x86_64"

    def fake_download(url: str, target: Path) -> None:
        """Write a release archive holding a fake binary."""
        with tarfile.open(target, "w:gz") as archive:
            info = tarfile.TarInfo(member)
            info.size = 4
            archive.addfile(info, io.BytesIO(b"\x7fELF"))

    monkeypatch.setattr(install, "download", fake_download)
    fetch_firecracker(tmp_path, "x86_64")
    assert (tmp_path / "firecracker").read_bytes() == b"\x7fELF"
    assert os.access(tmp_path / "firecracker", os.X_OK)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["firecracker"]


def test_setup_refuses_other_platforms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Not x86_64 Linux: a one-line error before anything is downloaded."""
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
    with pytest.raises(SetupError, match="x86_64 Linux"):
        install.setup(tmp_path, say=lambda _: None)
    assert not any(tmp_path.iterdir())


def test_the_mcp_server_without_vm_files_exits_with_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No traceback for an MCP client to show: the missing files and `chronobox setup`."""
    monkeypatch.setenv("CHRONOBOX_VM_DIR", str(tmp_path / "vm"))
    with pytest.raises(SystemExit) as stopped:
        server.main(["--root", str(tmp_path / "root")])
    assert str(stopped.value.code).startswith("chronobox-mcp: missing firecracker, vmlinux, tools.ext4")
    assert "chronobox setup" in str(stopped.value.code)
