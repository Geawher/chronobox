import os

import pytest

from chronobox.vm import VM_FILES, default_vm_dir


def _vm_available() -> bool:
    vm_dir = default_vm_dir()
    return os.access("/dev/kvm", os.R_OK | os.W_OK) and all((vm_dir / name).exists() for name in VM_FILES)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if _vm_available():
        return
    skip = pytest.mark.skip(reason="needs /dev/kvm and a built VM image (vm/fetch.sh, vm/build_rootfs.sh)")
    for item in items:
        if "vm" in item.keywords:
            item.add_marker(skip)
