"""节点引导的 Docker 安装分支；外部命令全部使用临时桩。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "addnode_template.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run_install(tmp_path: Path, *, assume_yes: bool = False, skip: bool = False, uid: int = 0, existing: bool = False):
    match = re.search(r"^install_docker_if_missing\(\) \{.*?^\}$", TEMPLATE.read_text(), re.M | re.S)
    assert match

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("mktemp", "rm", "chmod"):
        (bin_dir / name).symlink_to(shutil.which(name))
    _stub(bin_dir / "id", f'printf "%s\\n" {uid}\n')
    _stub(bin_dir / "wget", "exit 0\n")
    _stub(bin_dir / "systemctl", 'printf "systemctl %s\\n" "$*" >> "$MOCK_LOG"\n')
    if existing:
        _stub(bin_dir / "docker", '[ "$1" = "info" ]\n')
    _stub(
        bin_dir / "curl",
        'printf "curl %s\\n" "$*" >> "$MOCK_LOG"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    if [ "$1" = "-o" ]; then shift; printf "mock installer\\n" > "$1"; exit 0; fi\n'
        "    shift\n"
        "done\n"
        "exit 1\n",
    )
    _stub(
        bin_dir / "bash",
        'printf "bash %s\\n" "$*" >> "$MOCK_LOG"\n'
        'printf \'#!/bin/sh\\n[ "$1" = "info" ]\\n\' > "$MOCK_BIN/docker"\n'
        'chmod +x "$MOCK_BIN/docker"\n',
    )
    env = {**os.environ, "PATH": str(bin_dir), "MOCK_BIN": str(bin_dir), "MOCK_LOG": str(log)}
    env.pop("LLMPOD_BOOTSTRAP_ASSUME_YES", None)
    env.pop("LLMPOD_SKIP_DOCKER_INSTALL", None)
    if assume_yes:
        env["LLMPOD_BOOTSTRAP_ASSUME_YES"] = "1"
    if skip:
        env["LLMPOD_SKIP_DOCKER_INSTALL"] = "yes"
    result = subprocess.run(
        ["/bin/bash", "-c", match.group(0) + "\ninstall_docker_if_missing && docker info"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, log.read_text() if log.exists() else ""


def test_install_requires_confirmation_without_tty(tmp_path):
    result, calls = _run_install(tmp_path)
    assert result.returncode != 0
    assert "无法确认安装" in result.stdout
    assert calls == ""


def test_existing_docker_is_left_alone(tmp_path):
    result, calls = _run_install(tmp_path, existing=True)
    assert result.returncode == 0
    assert calls == ""


def test_install_can_be_disabled(tmp_path):
    result, calls = _run_install(tmp_path, assume_yes=True, skip=True)
    assert result.returncode != 0
    assert "请先安装 Docker" in result.stdout
    assert calls == ""


def test_install_requires_root(tmp_path):
    result, calls = _run_install(tmp_path, assume_yes=True, uid=1000)
    assert result.returncode != 0
    assert "需要 root" in result.stdout
    assert calls == ""


def test_install_uses_bt_script_then_checks_docker(tmp_path):
    result, calls = _run_install(tmp_path, assume_yes=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "https://download.bt.cn/install/0/docker_install.sh" in calls
    assert "install mirrors.aliyun.com/docker-ce" in calls
    assert "systemctl enable --now docker" in calls
