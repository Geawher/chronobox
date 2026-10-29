import os

import pytest

from chronobox.images import DEFAULT_IMAGE, image_disk
from chronobox.vm import VM_FILES, default_vm_dir


def _vm_available() -> bool:
    """Whether real-VM tests can run: KVM is usable and the VM files and default image are built."""
    vm_dir = default_vm_dir()
    files = [vm_dir / name for name in VM_FILES] + [image_disk(vm_dir, DEFAULT_IMAGE)]
    return os.access("/dev/kvm", os.R_OK | os.W_OK) and all(path.exists() for path in files)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip every test marked ``vm`` when no VM can run here, with what to build (or stop, when
    ``CHRONOBOX_REQUIRE_VM`` says they must run)."""
    if _vm_available():
        return
    reason = "needs /dev/kvm and the VM files (`chronobox setup`; `chronobox doctor` says what is missing)"
    if os.environ.get("CHRONOBOX_REQUIRE_VM"):
        raise pytest.UsageError(f"CHRONOBOX_REQUIRE_VM is set, but the real-VM tests cannot run: {reason}")
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "vm" in item.keywords:
            item.add_marker(skip)
