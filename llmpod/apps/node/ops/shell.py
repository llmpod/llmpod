"""Shell 操作：执行命令。"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex

from llmpod.apps.node.exceptions import TaskExecutionError
from llmpod.tunnel.schemas import ShellResult

log = logging.getLogger("llmpod.apps.node.ops.shell")

# 单流输出上限（字节）：RPC 触发的命令可能狂喷输出（如 cat 大文件），不设限会把
# 节点内存吃穿——通信帧经隧道 JSON 序列化还会再放大。超限截头保尾（末尾的错误
# 信息通常比开头更有用），并标注截断量让调用方能察觉
MAX_STREAM_BYTES = 4 * 1024 * 1024


def _clip(data: bytes) -> tuple[str, bool]:
    """解码并按 ``MAX_STREAM_BYTES`` 截断（保尾）；返回 (文本, 是否截断)。"""
    if len(data) > MAX_STREAM_BYTES:
        head_keep = MAX_STREAM_BYTES // 4
        marker = "\n...[输出超限截断]...\n".encode()
        clipped = data[:head_keep] + marker + data[-(MAX_STREAM_BYTES - head_keep) :]
        return clipped.decode(errors="replace"), True
    return data.decode(errors="replace"), False


async def execute_shell(
    command: str,
    timeout: int = 60,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> ShellResult:
    """异步执行 shell 命令。返回 :class:`~llmpod.tunnel.schemas.ShellResult`。"""
    log.info("Executing shell: %s (timeout=%ds)", command, timeout)

    # shlex 仅做分词（不经过 shell 解释，无注入面）
    cmd = shlex.split(command)

    env = {**os.environ, **env} if env else None

    proc = await asyncio.subprocess.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TaskExecutionError(f"命令执行超时（{timeout} 秒）")

    out_text, out_clipped = _clip(stdout)
    err_text, err_clipped = _clip(stderr)
    if out_clipped or err_clipped:
        log.warning("shell 输出超限截断（stdout=%d stderr=%d 字节）", len(stdout), len(stderr))
    return ShellResult(
        exit_code=proc.returncode,
        stdout=out_text,
        stderr=err_text,
    )
