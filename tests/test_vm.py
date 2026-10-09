from pathlib import Path

import pytest

from chronobox import vm
from chronobox.vm import default_vm_dir


def test_vm_dir_prefers_the_environment_then_a_built_checkout_then_the_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHRONOBOX_VM_DIR", str(tmp_path / "configured"))
    assert default_vm_dir() == tmp_path / "configured"
    monkeypatch.delenv("CHRONOBOX_VM_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    checkout = Path(vm.__file__).resolve().parents[2] / "vm" / "build"
    expected = checkout if checkout.is_dir() else tmp_path / "data" / "chronobox" / "vm"
    assert default_vm_dir() == expected
