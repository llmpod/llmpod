"""GPU 驱动/设备预检辅助（``gpu/probe.py``）测试。

用 tmp_path 伪造 sysfs / /dev 目录树，不依赖真实硬件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llmpod.apps.node.collectors.gpu import probe


@pytest.fixture
def fake_env(monkeypatch, tmp_path: Path):
    """把 PCI sysfs / 发行版标识 / /dev 指到临时目录，返回造设备的工厂。"""
    root = tmp_path / "pci"
    root.mkdir()
    monkeypatch.setattr(probe, "_PCI_DEVICES_DIR", root)
    monkeypatch.setattr(probe, "_OS_RELEASE", tmp_path / "os-release")  # 不存在 → 非 WSL
    monkeypatch.setattr(probe, "_KERNEL_VERSION", tmp_path / "version")

    def add_device(name: str, vendor: str) -> None:
        dev = root / name
        dev.mkdir()
        (dev / "vendor").write_text(f"{vendor}\n")

    return root, add_device


def test_pci_vendor_present_matches(fake_env):
    _, add_device = fake_env
    add_device("0000:01:00.0", "0x10de")
    add_device("0000:02:00.0", "0x8086")

    assert probe.pci_vendor_present("0x10de") is True
    assert probe.pci_vendor_present("0x19e5") is False


def test_pci_vendor_present_is_case_insensitive(fake_env):
    _, add_device = fake_env
    add_device("0000:01:00.0", "0x10DE")

    assert probe.pci_vendor_present("0x10de") is True


def test_pci_vendor_present_without_sysfs(monkeypatch, tmp_path: Path):
    """sysfs 不存在 → 返回 None（无法预检，调用方应跳过检查而不是判定无卡）。"""
    monkeypatch.setattr(probe, "_PCI_DEVICES_DIR", tmp_path / "missing")
    monkeypatch.setattr(probe, "_OS_RELEASE", tmp_path / "os-release")
    monkeypatch.setattr(probe, "_KERNEL_VERSION", tmp_path / "version")

    assert probe.pci_vendor_present("0x10de") is None


def test_pci_vendor_present_skips_wsl(monkeypatch, tmp_path: Path):
    """WSL 的 PCI sysfs 不反映真实 GPU，直接跳过预检。"""
    os_release = tmp_path / "os-release"
    os_release.write_text('NAME="Ubuntu"\nID=ubuntu\n')
    monkeypatch.setattr(probe, "_PCI_DEVICES_DIR", tmp_path)
    monkeypatch.setattr(probe, "_OS_RELEASE", os_release)
    monkeypatch.setattr(probe, "_KERNEL_VERSION", tmp_path / "version")

    assert probe.is_wsl() is False  # 纯发行版标识不算 WSL

    os_release.write_text('NAME="Ubuntu"\nID=ubuntu\n')
    (tmp_path / "version").write_text("Linux version 5.15.90.1-microsoft-standard-WSL2\n")
    assert probe.is_wsl() is True  # 微软内核缀是 WSL 的判据
    assert probe.pci_vendor_present("0x10de") is None


def test_pci_vendor_present_empty_dir(fake_env):
    assert probe.pci_vendor_present("0x10de") is False


def test_device_nodes_uses_caller_patterns(monkeypatch, tmp_path: Path):
    """模式由调用方给，本模块不认厂商——换一套模式就换一种节点。"""
    dev = tmp_path / "dev"
    dev.mkdir()
    for name in ("davinci0", "davinci1", "davinci_manager", "vdavinci0", "null"):
        (dev / name).touch()
    monkeypatch.setattr(probe, "_DEV_DIR", dev)

    ascend_names = [Path(node).name for node in probe.device_nodes(("davinci[0-9]*", "vdavinci[0-9]*"))]
    assert ascend_names == ["davinci0", "davinci1", "vdavinci0"]

    other_names = [Path(node).name for node in probe.device_nodes(("null",))]
    assert other_names == ["null"]


def test_device_nodes_dedups_overlapping_patterns(monkeypatch, tmp_path: Path):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "davinci0").touch()
    monkeypatch.setattr(probe, "_DEV_DIR", dev)

    assert len(probe.device_nodes(("davinci*", "davinci[0-9]*"))) == 1


def test_device_nodes_empty(monkeypatch, tmp_path: Path):
    dev = tmp_path / "dev"
    dev.mkdir()
    monkeypatch.setattr(probe, "_DEV_DIR", dev)

    assert probe.device_nodes(("davinci[0-9]*",)) == []
