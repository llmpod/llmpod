"""addnode NVIDIA 驱动安装分支测试；不联网、不装驱动、不碰宿主 /var/tmp。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "addnode_template.sh"

FUNCS = (
    "prompt_install_driver",
    "install_nvidia_driver_and_verify",
    "handle_missing_nvidia_driver",
)


def _extract(source: str) -> str:
    parts = []
    for name in FUNCS:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$", source, re.M | re.S)
        assert match, name
        parts.append(match.group(0))
    return "\n".join(parts)


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    body: str,
    *,
    installer_rc: str = "0",
    driver_ready: bool = False,
    curl_ok: bool = True,
    with_curl: bool = True,
    env_extra: dict[str, str] | None = None,
):
    functions = _extract(TEMPLATE.read_text())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ready_marker = tmp_path / "driver-ready"
    if driver_ready:
        ready_marker.touch()
    calls = tmp_path / "calls.log"

    for name in ("rm", "id"):
        (bin_dir / name).symlink_to(shutil.which(name))
    # mktemp 建到测试目录，避免落宿主 /var/tmp。
    _stub(
        bin_dir / "mktemp",
        'f="$MOCK_TMP/nvidia-install.$$.sh"\n: > "$f"\nprintf "%s\\n" "$f"\n',
    )
    _stub(
        bin_dir / "curl",
        '[ "$MOCK_CURL_OK" = "1" ] || exit 1\n'
        "out= prev=\n"
        'for a in "$@"; do\n'
        '    [ "$prev" = "-o" ] && out="$a"\n'
        '    prev="$a"\n'
        "done\n"
        'printf "#!/bin/sh\\nexit %s\\n" "$MOCK_INSTALLER_RC" > "$out"\n',
    )
    _stub(
        bin_dir / "bash",
        'printf "bash %s\\n" "$*" >> "$MOCK_LOG"\n'
        # --yes 由 addnode 传入：安装器内部二次确认跳过，版本菜单保留。
        'case " $* " in *" --yes "*) ;; *) printf "MISSING --yes\\n" >> "$MOCK_LOG";; esac\n'
        'exit "$MOCK_INSTALLER_RC"\n',
    )
    _stub(
        bin_dir / "nvidia-smi",
        '[ -f "$MOCK_READY" ] || exit 1\nprintf "GPU 0\\n"\n',
    )

    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "MOCK_TMP": str(tmp_path),
        "MOCK_LOG": str(calls),
        "MOCK_READY": str(ready_marker),
        "MOCK_CURL_OK": "1" if curl_ok else "0",
        "MOCK_INSTALLER_RC": installer_rc,
    }
    env.pop("LLMPOD_BOOTSTRAP_ASSUME_YES", None)
    env.pop("LLMPOD_SKIP_NVIDIA_DRIVER_INSTALL", None)
    if not with_curl:
        (bin_dir / "curl").unlink()
    if env_extra:
        env.update(env_extra)

    # NVIDIA_DRIVER_OK 由调用方在 handle 返回后设置，独立测试中显式初始化。
    script = functions + '\nexec 3<&0\nNVIDIA_DRIVER_OK=0 GPU_SKIP_REASON=""\n' + body
    result = subprocess.run(
        ["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=15
    )
    return result, calls.read_text() if calls.exists() else ""


def test_exit20_but_driver_works_continues(tmp_path):
    """退出码 20 不等于必须重启：实测 nvidia-smi 已通就直接继续。"""
    result, calls = _run(
        tmp_path, "install_nvidia_driver_and_verify", installer_rc="20", driver_ready=True
    )
    assert result.returncode == 0
    assert "NVIDIA 驱动已就绪" in result.stdout
    assert "--install --yes" in calls
    assert "MISSING --yes" not in calls


def test_exit20_without_driver_stops_with_rerun_hint(tmp_path):
    result, calls = _run(
        tmp_path, "install_nvidia_driver_and_verify", installer_rc="20", driver_ready=False
    )
    assert result.returncode == 1
    assert "重启" in result.stdout
    assert "重新执行同一条引导命令" in result.stdout


def test_exit0_without_driver_is_cancel(tmp_path):
    """取消也返回 0：以实测为准，驱动不在就按未完成退出。"""
    result, _ = _run(
        tmp_path, "install_nvidia_driver_and_verify", installer_rc="0", driver_ready=False
    )
    assert result.returncode == 1
    assert "退出码 0" in result.stdout


def test_failed_install_reports_exit_code(tmp_path):
    result, _ = _run(
        tmp_path, "install_nvidia_driver_and_verify", installer_rc="7", driver_ready=False
    )
    assert result.returncode == 1
    assert "退出码 7" in result.stdout


def test_download_failure_cleans_up(tmp_path):
    result, calls = _run(
        tmp_path,
        "install_nvidia_driver_and_verify",
        curl_ok=False,
    )
    assert result.returncode == 1
    assert "下载失败" in result.stdout
    assert calls == ""
    assert list(tmp_path.glob("nvidia-install.*")) == []


def test_missing_curl_stops_before_mktemp(tmp_path):
    result, calls = _run(
        tmp_path, "install_nvidia_driver_and_verify", with_curl=False
    )
    assert result.returncode == 1
    assert "找不到 curl" in result.stdout
    assert list(tmp_path.glob("nvidia-install.*")) == []


def test_assume_yes_refuses_auto_driver_install(tmp_path):
    result, _ = _run(
        tmp_path,
        "prompt_install_driver",
        env_extra={"LLMPOD_BOOTSTRAP_ASSUME_YES": "1"},
    )
    assert result.returncode == 1
    assert "非交互" in result.stdout


def test_skip_env_marks_gpu_skipped(tmp_path):
    result, calls = _run(
        tmp_path,
        'handle_missing_nvidia_driver\necho "SKIP=[$GPU_SKIP_REASON] OK=$NVIDIA_DRIVER_OK"',
        env_extra={"LLMPOD_SKIP_NVIDIA_DRIVER_INSTALL": "yes"},
    )
    assert result.returncode == 0
    assert "SKIP=[已跳过 NVIDIA 驱动安装]" in result.stdout
    assert "OK=0" in result.stdout
    assert calls == ""
