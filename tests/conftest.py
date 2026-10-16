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
    """Skip every test marked ``vm`` when no VM can run here, with what to build."""
    if _vm_available():
        return
    skip = pytest.mark.skip(
        reason="needs /dev/kvm, the tools drive and the default image (vm/fetch.sh, vm/build_rootfs.sh)"
    )
    for item in items:
        if "vm" in item.keywords:
            item.add_marker(skip)
