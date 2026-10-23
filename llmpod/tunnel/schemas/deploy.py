"""DeploySpec：runtime 无关的可移植部署规格（wire 入参契约）。

引擎适配器（Manager 侧）产出一份 DeploySpec，执行器（Node 侧 ``Deployer``）消费一份——
让「引擎知识」与「runtime 知识」正交分离：同一份 DeploySpec 可被 ``DockerDeployer`` /
将来的 ``K8sDeployer`` 各自翻译成目标 runtime 调用。

字段集对齐现网 ``build_run_spec``（``orchestrator.py`` 模块级函数）产出的 spec dict，
仅正式命名 + 类型化，不改语义。wire 上仍以 dict（JSON）传输；Node 侧 ``DockerDeployer.deploy`` 收到后
``model_validate`` 归一为 DeploySpec（顺带做边界校验）。

与 ``tunnel/schemas/container.py`` 的 ``DeployResult`` 等出参契约同属 wire 层：本文件为
**入参**契约（``ContainerService.deploy`` 的形参），故与其他 wire model 同住 ``schemas/``，
统一由 ``tunnel/schemas/__init__.py`` re-export。
"""

from __future__ import annotations

import shlex

from pydantic import BaseModel, ConfigDict, Field


class DeploySpec(BaseModel):
    """单个推理引擎参数的可移植启动规格（runtime 无关）。"""

    model_config = ConfigDict(extra="forbid")

    image: str = Field(..., description="容器镜像，如 vllm/vllm-openai:v0.23.0")
    name: str = Field(..., description="工作负载名（幂等 + label 反查用）：llmpod-{dep[:8]}-{inst}")
    entrypoint: list[str] | None = Field(None, description="覆盖镜像 ENTRYPOINT（list）")
    args: list[str] | None = Field(None, description="启动参数（跟在 ENTRYPOINT 后）")
    env: dict[str, str] | None = Field(None, description="环境变量")
    gpus: list[int] | None = Field(
        None,
        description="GPU 设备索引，如 [0, 1]；[] 空表=全部 GPU（docker count=-1）；None=不分配 GPU（不带 device_requests）",
    )
    ports: dict[str, int] | None = Field(None, description="端口映射 {host: cont}")
    volumes: dict[str, str] | None = Field(None, description="数据卷 {host: container}")
    driver_mounts: list[str] | None = Field(None, description="驱动目录/so 挂载 ['host:cont[:mode]']")
    labels: dict[str, str] | None = Field(None, description="标签（value 必须 string）")
    network_mode: str | None = Field(None, description="网络模式，如 host / bridge")
    ipc: str | None = Field(None, description="IPC 模式（docker→ipc_mode）")
    shm_size: str | None = Field(None, description="/dev/shm 大小，如 16g")
    runtime: str | None = Field(None, description="docker runtime，如 nvidia / ascend")
    privileged: bool = Field(False, description="特权模式（默认 False，仅兜底）")
    cap_add: list[str] | None = Field(None, description="Linux capabilities")
    extra_devices: list[str] | None = Field(None, description="额外设备 ['/dev/x:/dev/x:rwm']（docker→devices）")
    restart_policy: str | None = Field(None, description="重启策略 no / always / ...")


def _flag_value(flag: str, value: str) -> str:
    """`--flag value`，value 含 shell 元字符时加引号（docker run 视觉还原）。"""
    return f"{flag} {shlex.quote(value)}"


def docker_run_command(spec: DeploySpec) -> str:
    """DeploySpec → 完整 ``docker run`` 命令行（人读 + 可复制执行）。

    翻译规则镜像 Node 侧 ``DockerDeployer._spec_to_run_kwargs``（apps/node/deployers/
    docker.py，spec→容器 的权威实现）：那边把 spec 翻成 SDK kwargs，这边翻成等价 CLI
    flags——**改 DeploySpec 字段或 deployer 映射时必须同步此函数**（测试有逐字段覆盖
    与 kwargs 交叉核对兜底）。已知近似：模型卷 host 前缀改写（``_rewrite_model_host_path``，
    仅引导参数 model_host_dir 非默认时生效）发生在 Node 侧，此处按 spec 原始键渲染。
    """
    parts: list[str] = ["docker run -d"]

    parts.append(_flag_value("--name", spec.name))
    if spec.runtime:
        parts.append(_flag_value("--runtime", spec.runtime))
    if spec.network_mode:
        parts.append(_flag_value("--network", spec.network_mode))
    if spec.ipc:
        parts.append(_flag_value("--ipc", spec.ipc))
    if spec.shm_size:
        parts.append(_flag_value("--shm-size", spec.shm_size))
    if spec.restart_policy:
        parts.append(_flag_value("--restart", spec.restart_policy))
    if spec.privileged:
        parts.append("--privileged")
    for cap in spec.cap_add or []:
        parts.append(_flag_value("--cap-add", cap))
    for dev in spec.extra_devices or []:
        parts.append(_flag_value("--device", dev))

    # GPU：None=不分配；[] 空表=全部 GPU；[0,1]=指定设备（deployer _parse_gpus 同语义）
    if spec.gpus is not None:
        if len(spec.gpus) == 0:
            parts.append("--gpus all")
        else:
            parts.append(f'--gpus "device={",".join(str(g) for g in spec.gpus)}"')

    for host, cont in (spec.ports or {}).items():
        parts.append("-p " + shlex.quote(f"{host}:{cont}"))
    for host, cont in (spec.volumes or {}).items():
        parts.append("-v " + shlex.quote(f"{host}:{cont}"))
    for mount in spec.driver_mounts or []:
        parts.append("-v " + shlex.quote(mount))
    for key, value in (spec.labels or {}).items():
        parts.append(_flag_value("--label", f"{key}={value}"))
    for key, value in (spec.env or {}).items():
        parts.append("-e " + shlex.quote(f"{key}={value}"))

    parts.append(shlex.quote(spec.image))
    # entrypoint+args 即容器进程 argv（= docker inspect 的 Entrypoint+Cmd）。
    # 不用 --entrypoint flag：CLI 只吃单值，SGLang 的 ["python3","-m",...] 多元素表达不了。
    for token in (*(spec.entrypoint or []), *(spec.args or [])):
        parts.append(shlex.quote(token))

    return " \\\n  ".join(parts)
