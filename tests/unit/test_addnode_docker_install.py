"""节点引导的 Docker 安装分支；外部命令全部使用临时桩。"""

from __future__ import annotations

import os
import re
import shlex
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
    # 清理函数打桩：既不碰宿主 /etc/apt，也能断言安装流程确实调用了它。
    script = (
        match.group(0)
        + '\nclean_invalid_docker_apt_list() { printf "cleanup %s\\n" "${1:-default}" >> "$MOCK_LOG"; }\n'
        + "\ninstall_docker_if_missing && docker info"
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, log.read_text() if log.exists() else ""


def _run_clean(tmp_path: Path, content: str):
    """独立跑 docker.list 清理函数，传参指向临时文件，不碰宿主 /etc/apt。"""
    source = TEMPLATE.read_text()
    match = re.search(r"^clean_invalid_docker_apt_list\(\) \{.*?^\}$", source, re.M | re.S)
    assert match
    target = tmp_path / "docker.list"
    target.write_text(content)
    result = subprocess.run(
        ["/bin/bash", "-c", match.group(0) + f"\nclean_invalid_docker_apt_list {shlex.quote(str(target))}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, target, tmp_path / "docker.list.invalid"


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
    # 源参数必须带协议前缀：BT 脚本原样拼进 deb 源行，裸主机名会写出
    # 非法 docker.list 拖垮整机 apt（Ubuntu 22 实测）。
    assert "install https://mirrors.aliyun.com/docker-ce" in calls
    assert "cleanup default" in calls
    assert "systemctl enable --now docker" in calls


def test_clean_moves_schemeless_docker_list(tmp_path):
    """无协议前缀的 docker.list 被移开，原名保留 .invalid 供排查。"""
    result, target, invalid = _run_clean(
        tmp_path, "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] mirrors.aliyun.com/docker-ce/linux/ubuntu jammy stable\n"
    )
    assert result.returncode == 0
    assert "警告" in result.stdout
    assert not target.exists()
    assert invalid.exists()


def test_clean_keeps_valid_https_list(tmp_path):
    result, target, invalid = _run_clean(
        tmp_path, "deb [arch=amd64] https://mirrors.aliyun.com/docker-ce/linux/ubuntu jammy stable\n"
    )
    assert result.returncode == 0
    assert target.exists()
    assert not invalid.exists()


def test_clean_keeps_file_mirror_list(tmp_path):
    """本地 file:// 镜像源是合法形态，不能被清掉（离线环境依赖）。"""
    result, target, invalid = _run_clean(
        tmp_path, "deb [arch=amd64] file:///opt/mirror/docker-ce/linux/ubuntu jammy stable\n"
    )
    assert result.returncode == 0
    assert target.exists()
    assert not invalid.exists()


def test_clean_ignores_missing_file(tmp_path):
    result, target, invalid = _run_clean(tmp_path, "")
    target.unlink()
    assert result.returncode == 0
    assert result.stdout == ""
    assert not invalid.exists()
