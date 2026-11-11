"""完整启动命令模板：占位符校验、逐实例渲染与拆分。

用户在部署配置里可以直接给一条完整命令（``DeploymentConfig.command``）。部署期平台把
``{model_path}`` 等占位符替换成实例的真实值后执行；不填时仍由适配器按引擎规则生成参数。
占位符表是平台与用户之间的契约：新增占位符要同时改这里与适配器的回显映射
（``EngineAdapter.command_placeholder_flags``）。
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from llmpod.core.exceptions import LLMPodValidationError

if TYPE_CHECKING:
    from llmpod.apps.manager.deployment.engine.base import EngineAdapter
    from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology


# 占位符 → 人读说明（错误信息与 inspect 回显共用；只在此处登记）
COMMAND_PLACEHOLDER_HINTS: dict[str, str] = {
    "model_path": "模型权重在节点上的路径",
    "served_model_name": "对外模型名",
    "port": "平台按节点分配的服务端口（必填，就绪探活用它）",
    "tp": "张量并行度",
    "pp": "流水线并行度",
    "nnodes": "本引擎跨机节点数",
    "node_rank": "本分片在引擎内的序号（head 为 0）",
    "master_addr": "多机 rendezvous 主节点地址（仅 nnodes>1）",
    "master_port": "多机 rendezvous 主端口（仅 nnodes>1）",
    "headless": "worker 分片自动加 --headless（head 分片渲染为空）",
}

# 占位符清单的人读文本（Swagger 描述共用，避免两处手写清单漂移）
COMMAND_PLACEHOLDER_DOC = "、".join("{" + name + "}" for name in COMMAND_PLACEHOLDER_HINTS)

# 前面带 $ 的 {...} 是 shell 变量，不按平台占位符处理
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")

# shell 运算符：``shlex`` 开 punctuation_chars 后，未用空格分隔的 ``>/tmp/x``、
# ``2>&1``、``|tee`` 也会拆成独立 token
_SHELL_PUNCTUATION = frozenset({"&", "|", ";", "<", ">", "(", ")"})
# $VAR / ${VAR} / $(cmd) / $(...)：出现即说明命令依赖 shell 展开
_SHELL_EXPANSION_RE = re.compile(r"\$[A-Za-z_{(`]")


def command_placeholder_names(command: str) -> list[str]:
    """命令模板里出现的占位符名（保持出现序，去重；``${SHELL_VAR}`` 不算）。"""
    names: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(command):
        name = match.group("name")
        if name not in names:
            names.append(name)
    return names


def validate_command_template(
    command: str | None,
    *,
    has_multinode: bool,
    required: list[str] | None = None,
) -> None:
    """校验完整命令模板；不合法 raise LLMPodValidationError。

    - 必填 ``{port}``：平台按节点分配服务端口并用它做就绪探活，命令里不写平台就
      不知道探哪个口；
    - 未知占位符一律拒（拼错 ``{model_paht}`` 不能静默原样传给引擎）；
    - ``{master_addr}`` / ``{master_port}`` 只在部署含多机时可用；
    - ``required`` 是引擎声明这条命令必须带的占位符（如 vLLM 多机 worker 的
      ``{headless}``），缺了引擎会跑错。
    """
    if command is None:
        return
    if not command.strip():
        raise LLMPodValidationError("完整启动命令不能为空；不填则按引擎默认生成")
    command_argv(command)
    names = command_placeholder_names(command)
    if required:
        missing = [name for name in required if name not in names]
        if missing:
            given = "、".join(f"{{{name}}}" for name in missing)
            raise LLMPodValidationError(f"完整启动命令缺少引擎必需的占位符：{given}")
    unknown = [name for name in names if name not in COMMAND_PLACEHOLDER_HINTS]
    if unknown:
        supported = "、".join(f"{{{name}}}" for name in COMMAND_PLACEHOLDER_HINTS)
        given = "、".join(f"{{{name}}}" for name in unknown)
        raise LLMPodValidationError(f"完整启动命令含未知占位符：{given}（支持：{supported}）")
    if "port" not in names:
        raise LLMPodValidationError(
            "完整启动命令必须包含 {port} 占位符（平台用它回填服务端口并就绪探活）"
        )
    if not has_multinode:
        multi = [name for name in names if name in ("master_addr", "master_port")]
        if multi:
            given = "、".join(f"{{{name}}}" for name in multi)
            raise LLMPodValidationError(f"完整启动命令含多机占位符（{given}），但当前部署没有多机单元")


def validate_command_for_topology(command: str | None, topology: "Topology", adapter: "EngineAdapter") -> None:
    """按部署拓扑推导多机/节点数约束后校验完整命令模板（create / inspect / _plan 共用）。"""
    unit_nnodes = topology.nnodes_values()
    if command is not None and len(unit_nnodes) > 1:
        values = "、".join(str(v) for v in sorted(unit_nnodes))
        raise LLMPodValidationError(f"完整启动命令要求各副本的跨机节点数一致（当前：{values}）")
    has_multinode = any(nnodes > 1 for nnodes in unit_nnodes)
    validate_command_template(
        command,
        has_multinode=has_multinode,
        required=adapter.multinode_command_placeholders if has_multinode else [],
    )


def render_command_template(command: str, inst: "InstanceSpec", adapter: "EngineAdapter") -> str:
    """按实例替换占位符，返回 shell 命令串（是否经 shell 执行由调用方判定）。

    通用占位符在这里取值；引擎特有占位符（如 vLLM 的 ``{headless}``）由适配器
    ``command_placeholder_values`` 提供。``${...}`` 原样保留给 shell；值按 shell
    规则加引号，用户自己已用单/双引号包住占位符时不再重复加。
    """
    # 先按登记的占位符铺空值：引擎不适用的占位符（如非 vLLM 命令里写了
    # {headless}）渲染成空串，不能把 {...} 字面量漏给容器
    values = {name: "" for name in COMMAND_PLACEHOLDER_HINTS}
    values.update(
        {
            "model_path": str(inst.model_path or ""),
            "served_model_name": str(inst.served_model_name or ""),
            "port": str(inst.port or ""),
            "tp": str(inst.tp),
            "pp": str(inst.pp),
            "nnodes": str(inst.nnodes),
            "node_rank": str(inst.node_rank),
            "master_addr": str(inst.master_addr or ""),
            "master_port": str(inst.master_port or ""),
        }
    )
    values.update(adapter.command_placeholder_values(inst))
    rendered = command
    for name, value in values.items():
        rendered = _substitute_placeholder(rendered, name, value)
    return rendered


def _substitute_placeholder(command: str, name: str, value: str) -> str:
    """替换单个占位符；空值直接消失，值含空白/shell 特殊字符时加引号。"""
    pattern = re.compile(r"(?<!\$)\{" + name + r"\}")
    if not value:
        # 占位符被引号包住时连引号一起去掉，避免留下空参数 ""；引号内的模型路径
        # 等值在非空分支同样由 _replace 处理
        quoted = re.compile(r"(['\"])" + re.escape("{" + name + "}") + r"\1")
        return pattern.sub("", quoted.sub("", command))

    def _replace(match: re.Match[str]) -> str:
        before = command[match.start() - 1] if match.start() > 0 else ""
        after = command[match.end()] if match.end() < len(command) else ""
        if before in ("'", '"') and after == before:
            return value
        return shlex.quote(value)

    return pattern.sub(_replace, command)


def command_argv(rendered: str) -> list[str]:
    """把渲染后的命令拆成 argv（不经 shell 执行时用）。"""
    try:
        return shlex.split(rendered)
    except ValueError as e:
        raise LLMPodValidationError(f"完整启动命令无法解析：{e}") from e


def join_command(tokens: list[str]) -> str:
    """把 argv 拼成可编辑的命令串：平台占位符不加引号，其余按 shell 规则转义。

    ``shlex.join`` 会把 ``{model_path}`` 整体加上引号，用户看到的是
    ``'--model' '{model_path}'``，既难读也容易在手工编辑时丢引号；这里含占位符的
    token 原样保留，其余一律 ``shlex.quote``（安全 token 会原样返回）。
    """
    parts: list[str] = []
    for token in tokens:
        if _PLACEHOLDER_RE.search(token):
            parts.append(token)
        else:
            parts.append(shlex.quote(token))
    return " ".join(parts)


def command_needs_shell(rendered: str) -> bool:
    """命令是否带 shell 语法（管道 / 重定向 / 变量展开 / 命令替换）。

    带的话整条交给 ``bash -c``（用户自己写的 ``source env.sh && ...`` 才能生效）；
    不带就按 argv 直接 exec，引擎进程是 PID 1，docker stop 的信号能直接到引擎。
    """
    if "`" in rendered:
        return True
    if _SHELL_EXPANSION_RE.search(rendered):
        return True
    try:
        lexer = shlex.shlex(rendered, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # 引号不配对等非法写法交给 shell 报错，别在平台侧拼一个半截命令
        return True
    # 只认「纯运算符」token（``>`` / ``&&`` / ``>&``）；引号里的 ``a&b`` 不是
    # 运算符，``shlex`` 会把它保持成单个普通 token
    return any(token and all(ch in _SHELL_PUNCTUATION for ch in token) for token in tokens)
