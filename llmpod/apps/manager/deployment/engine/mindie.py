"""MindIE 引擎适配器（昇腾官方推理引擎，cann）。

MindIE 是**文件驱动引擎**：不收 CLI 参数，靠 config.json + ``mindieservice_daemon``
启动——与 vllm/sglang 的 CLI 参数型完全不同。``generate_start_args`` 产出两个
positional token ``[引导脚本, "bash"]``，经 entrypoint ``/bin/bash -c`` 拼成容器 argv
``/bin/bash -c <脚本> bash <用户flags...>``：

- ``args[1]="bash"`` 是 ``bash -c`` 的 **$0 占位**——没有它第一个用户 flag 会被 bash
  当脚本名吃掉；用户 extra_args 经三层合并落到尾部，由脚本里的 ``"$@"`` 透传 daemon。
- 引导脚本：heredoc（引号定界符，零展开）落盘 config.json → ``chmod 640``（出厂模板
  同为 640）→ best-effort source CANN/ATB/MindIE 环境（显式覆盖镜像 ENTRYPOINT，其
  原入口的环境初始化由这里补偿）→ ``exec daemon --config-file <conf> "$@"``。
- config 结构对齐 3.1.0 出厂模板（M1 真机核验）：``ModelConfig`` 嵌套于
  ``BackendConfig.ModelDeployConfig`` 下、worldSize 只在模型条目上（BackendConfig
  无此键）、daemon 只认 ``--config-file``（无 ``--config``/``-c``）。系统层写死
  ``httpsEnabled/interCommTLSEnabled/interNodeTLSEnabled:false``（平台是 http 反代、
  不挂证书）与 ``ipAddress:0.0.0.0 + allowAllZeroIpListening:true``（出厂 127.0.0.1
  容器外不可达）；management/metrics 端口由业务端口 +1/+2 派生（host 网络下同节点
  多实例不撞固定端口）。

健康检查用业务口 ``/v1/models``（M1 真机实测）：MindIE 的权威就绪探针 ``/v2/health/ready``
只在管理口（``managementPort=port+1``，绑 127.0.0.2），而平台探活 URL 由 reconciler 固定按
业务口拼装、适配器只能声明 path——业务口上唯一可探的就是 ``/v1/models``。其语义弱于权威
探针：实测「HTTP 起服务」早于「模型加载完」约 10~20s，该窗口内标 READY 放进的请求会失败，
由代理侧故障切换/端点冷却兜底。设备直通照抄 vLLM cann 分支（ascend runtime + 逐卡 davinci）。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from llmpod.apps.manager.deployment.engine.base import EngineAdapter
from llmpod.apps.manager.deployment.schemas import DeviceConfig, EngineConfig, InstanceSpec, Topology
from llmpod.config.enums import ModelType
from llmpod.core.exceptions import LLMPodValidationError

# 容器内路径/入口常量（MindIE Service 3.1.0 镜像实测：310P3 x86_64 / 驱动 25.2.3 / CANN 9.0.1）
_DAEMON_PATH = "/usr/local/Ascend/mindie/latest/mindie-service/bin/mindieservice_daemon"
_CONF_PATH = "/tmp/llmpod-mindie/config.json"
_SERVICE_HOME = "/usr/local/Ascend/mindie/latest/mindie-service"
# best-effort source 清单：CANN toolkit + ATB + MindIE 三层各自带 set_env.sh，
# 镜像内路径差异由 `|| true` 吸收（缺哪层跳哪层）
_ENV_SOURCES = (
    "/usr/local/Ascend/ascend-toolkit/set_env.sh",
    "/usr/local/Ascend/nnal/atb/set_env.sh",
    "/usr/local/Ascend/mindie/set_env.sh",
)
# torch 安装路径（镜像实测）：mindie-llm set_env.sh 用 python3 -c 'import torch' 探测
# 此路径，设备被节点代理占用（DCMI -8020）时探测仍会打印正确路径，但 CANN/driver 把
# 错误日志写进 **stdout**（2>/dev/null 挡不住），$(...) 连日志一起捕获成多行脏值 →
# torch/lib 拼出非法目录进 LD_LIBRARY_PATH，daemon 报 libtorch_cpu.so 缺失退出 127。
# 脏值非空，空值兜底（${VAR:-…}）拦不住，只能不信任探测结果、强制覆写为已核验路径
_TORCH_INSTALL_PATH = "/usr/local/lib64/python3.11/site-packages/torch"


def _bootstrap_script(conf: dict, model_path: str) -> str:
    """渲染落盘 config.json 并拉起 daemon 的 bash 脚本（``bash -c`` 的脚本体）。"""
    payload = json.dumps(conf, indent=2, ensure_ascii=False)
    script = (
        "set -e\n"
        "mkdir -p /tmp/llmpod-mindie\n"
        f"cat > {_CONF_PATH} <<'LLMPOD_CONF_EOF'\n{payload}\nLLMPOD_CONF_EOF\n"
        f"chmod 640 {_CONF_PATH}\n"
        # 310P 不支持 bfloat16（M1 实测：daemon 在模型加载期 NotImplementedError 崩溃），
        # Qwen 系官方权重 bf16 落盘——幂等改写模型 config.json 为 fp16（已是 fp16 则无操作）。
        # 模型卷 rw 挂载可写；意外只读时 || true 吸收，daemon 会以原始报错暴露
        f'sed -i \'s/"torch_dtype": *"bfloat16"/"torch_dtype": "float16"/\' {model_path}/config.json 2>/dev/null || true\n'
        + "".join(f"source {s} 2>/dev/null || true\n" for s in _ENV_SOURCES)
        # source 链会把 PYTORCH_INSTALL_PATH 覆盖成多行脏值（见 _TORCH_INSTALL_PATH
        # 注释），空值兜底拦不住非空的脏值——不信任探测结果，直接覆写并把 torch/lib
        # 注入链接路径（ld.so 会跳过脏目录）。设备真被占时 daemon 会在设备初始化处
        # 以真实错误暴露（不再是误导性的 127）
        + f"PYTORCH_INSTALL_PATH={_TORCH_INSTALL_PATH}\n"
        + "export PYTORCH_INSTALL_PATH\n"
        + 'export LD_LIBRARY_PATH="$PYTORCH_INSTALL_PATH/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        + f"cd {_SERVICE_HOME} 2>/dev/null || true\n"
        + f'exec {_DAEMON_PATH} --config-file {_CONF_PATH} "$@"\n'
    )
    return script


class MindIEAdapter(EngineAdapter):
    """MindIE 适配器（昇腾 NPU，310P 家族验收）。"""

    supported_formats = ["safetensors"]
    supported_gpu_archs = ["cann"]
    # 首期只声明已验证的 LLM chat（embedding/rerank 未验证，fail-closed 不声明）
    supported_categories = [ModelType.LLM]
    supported_operations = ["chat.completions"]
    supported_parallel = []  # 单机单组为主；tp 由 worldSize=npu 数表达，不设并行 flag
    supports_multinode = False  # 多机需 ranktable 编排且量化不支持多机
    trust_remote_code_flag = None  # 无 CLI flag——由 config 的 trustRemoteCode 表达
    supported_features = []
    # 系统必控 flag：config 路径由引导脚本持有，用户 extra_args 同名会被忽略
    # （daemon 只认 --config-file，无 --config/-c——M1 真机 strings 实证）
    protected_flags = ["--config-file"]
    param_suggestions = []  # MindIE 调参项是 config 字段非 CLI flag，不诱导用户塞 flag

    entrypoint = ["/bin/bash", "-c"]  # 显式覆盖镜像 ENTRYPOINT（脚本内补偿 source）

    # 容器/服务默认值（host 网络同 vLLM cann 先例）
    default_port = 1025  # MindIE Service 业务端口出厂值
    default_network_mode = "host"
    default_ipc = "host"
    default_shm_size = "16g"
    default_restart_policy = "no"

    supported_protocols = ["openai"]

    def topology_conflict(self, arch: str, topology) -> str | None:
        """cann（MindIE）拓扑硬冲突：多机与 pp>1 都不支持——与 generate_start_args
        的组装事实同口径（config 只会渲染单机组，放行只会拉起互不相识的容器各自挂死）。

        自动回落序据此剔除 cann；手选卡路径在创建期就拒（同 vLLM cann 判据）。
        """
        if arch != "cann" or topology is None:
            return None
        units = topology.per_copy_units()
        if any(int(u.nnodes or 1) > 1 for u in units):
            return "MindIE 暂不支持多机部署（多机需 ranktable 等额外编排，量化亦不支持多机）"
        if all(max(1, int(u.pp or 1)) <= 1 for u in units):
            return None
        return "MindIE 暂不支持流水线并行（pp>1）"

    @classmethod
    def build_device_config(cls, instance: InstanceSpec, gpu_arch: str) -> DeviceConfig:
        if gpu_arch == "cann":
            # 与 VLLMAdapter 的 cann 分支同构：ascend runtime + /dev/davinci* 直通 +
            # 宿主 driver/dcmi 只读挂载（gpus=None，不走 nvidia device_requests）
            npu_ids = list(instance.assigned_gpus or [0])
            return DeviceConfig(
                runtime="ascend",
                gpus=None,
                driver_mounts=[
                    "/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64:ro",
                    "/usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info:ro",
                    "/usr/local/dcmi:/usr/local/dcmi:ro",
                    "/usr/local/bin/npu-smi:/usr/local/bin/npu-smi:ro",
                    "/etc/ascend_install.info:/etc/ascend_install.info:ro",
                ],
                extra_devices=(
                    [f"/dev/davinci{i}:/dev/davinci{i}:rwm" for i in npu_ids]
                    + [
                        "/dev/davinci_manager:/dev/davinci_manager:rwm",
                        "/dev/devmm_svm:/dev/devmm_svm:rwm",
                        "/dev/hisi_hdc:/dev/hisi_hdc:rwm",
                    ]
                ),
                privileged=False,
                cap_add=[],
            )
        raise NotImplementedError(f"MindIE 不支持的软件栈：{gpu_arch}")

    @classmethod
    def build_env(
        cls,
        instance: InstanceSpec,
        topology: Topology,
        engine_config: EngineConfig,
        health_timeout_s: int = 600,
    ) -> dict:
        # 曝全部分配到的 NPU（与 build_device_config 的 davinci 挂载同口径）；npuDeviceIds
        # 用逻辑重编号（0..world-1），物理选卡由设备直通 + 本变量承担
        ids = ",".join(str(i) for i in (instance.assigned_gpus or [0]))
        return {
            "ASCEND_VISIBLE_DEVICES": ids,
            "ASCEND_RT_VISIBLE_DEVICES": ids,
            # daemon 日志默认写文件不进 stdout，容器日志会一片空白（M1 实测）；
            # 此变量把 CANN/mindie 日志改道 stdout，平台「容器日志」功能才看得到东西
            "ASCEND_SLOG_PRINT_TO_STDOUT": "1",
        }

    @classmethod
    def health_check(cls) -> tuple[str, Callable[[Any], bool]]:
        # 业务口上的事实唯一探针（M1 实测：/health、/healthz、/readyz 在业务口恒 404，
        # 权威探针 /v2/health/ready 只在管理口 127.0.0.2:port+1，平台探活 URL 够不着）。
        # 弱语义：/v1/models 比「模型加载完」早约 10~20s 返回 200，早放流量由代理侧
        # 故障切换/冷却兜底——接受该窗口是「不动 reconciler 共用逻辑」的取舍
        return "/v1/models", lambda r: getattr(r, "status_code", 0) == 200

    def generate_start_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_params: dict,
        *,
        engine_version: str | None = None,
    ) -> list[str]:
        # 判**本实例**自己的形状而非整拓扑（同 vLLM _ascend_args）：异构副本里兄弟份
        # 用 pp>1 不该把 pp=1 的 cann 份连带拒掉
        own = Topology(
            tp=max(1, int(instance.tp or 1)),
            pp=max(1, int(instance.pp or 1)),
            nnodes=max(1, int(instance.nnodes or 1)),
        )
        conflict = self.topology_conflict("cann", own)
        if conflict is not None:
            raise LLMPodValidationError(f"{conflict}；请单机部署或换 vllm 引擎")

        port = int(instance.port or self.default_port)
        world = len(instance.assigned_gpus or [0])
        served = instance.served_model_name or instance.model_path
        # 出厂模板**全量键集**，只覆写平台管控项（M1 真机核验教训：ConfigManager 按
        # 模板逐键取值，省略的键内部合并成 null，读到布尔键直接 type_error.302 崩溃，
        # 故 TLS 证书路径等惰性键也原样保留——TLS 三开关关闭时它们不被使用）。
        # 平台覆写：0.0.0.0 监听 + https/interComm/interNode TLS 关（http 反代）、
        # 派生端口、npuDeviceIds 逻辑重编号、模型四维、maxSeqLen=8192（出厂 2560
        # 过小）且约束 maxSeqLen = maxInputTokenLen + maxIterTimes（出厂同式）。
        conf = {
            "Version": "1.0.0",
            "ServerConfig": {
                "ipAddress": "0.0.0.0",
                "managementIpAddress": "127.0.0.2",
                "port": port,
                "managementPort": port + 1,  # 派生端口：host 网络下多实例不撞固定 1026/1027
                "metricsPort": port + 2,
                "allowAllZeroIpListening": True,
                "maxLinkNum": 1000,
                "httpsEnabled": False,
                "fullTextEnabled": False,
                "tlsCaPath": "security/ca/",
                "tlsCaFile": ["ca.pem"],
                "tlsCert": "security/certs/server.pem",
                "tlsPk": "security/keys/server.key.pem",
                "tlsCrlPath": "security/certs/",
                "tlsCrlFiles": ["server_crl.pem"],
                "managementTlsCaFile": ["management_ca.pem"],
                "managementTlsCert": "security/certs/management/server.pem",
                "managementTlsPk": "security/keys/management/server.key.pem",
                "managementTlsCrlPath": "security/management/certs/",
                "managementTlsCrlFiles": ["server_crl.pem"],
                "metricsTlsCaFile": ["metrics_ca.pem"],
                "metricsTlsCert": "security/certs/metrics/server.pem",
                "metricsTlsPk": "security/keys/metrics/server.key.pem",
                "metricsTlsCrlPath": "security/metrics/certs/",
                "metricsTlsCrlFiles": ["server_crl.pem"],
                "inferMode": "standard",
                "interCommTLSEnabled": False,
                "interCommPort": 1121,
                "interCommTlsCaPath": "security/grpc/ca/",
                "interCommTlsCaFiles": ["ca.pem"],
                "interCommTlsCert": "security/grpc/certs/server.pem",
                "interCommPk": "security/grpc/keys/server.key.pem",
                "interCommTlsCrlPath": "security/grpc/certs/",
                "interCommTlsCrlFiles": ["server_crl.pem"],
                "openAiSupport": "vllm",
                "tokenTimeout": 600,
                "e2eTimeout": 600,
                "distDPServerEnabled": False,
                "layerwiseDisaggregated": False,
                "layerwiseDisaggregatedRoleType": "",
                "layerwiseDisaggregatedMasterIpAddress": "127.0.0.3",
                "layerwiseDisaggregatedSlaveIpAddress": ["127.0.0.4"],
                "layerwiseDisaggregatedDataPort": 10024,
                "layerwiseDisaggregatedCrtlPort": [10001, 10002],
                "HealthCheckConfig": {"npuUsageThreshold": 0},
            },
            "BackendConfig": {
                "backendName": "mindieservice_llm_engine",
                "modelInstanceNumber": 1,
                # 逻辑 ID 0..world-1（物理选卡由设备直通 + ASCEND_RT_VISIBLE_DEVICES 承担）
                "npuDeviceIds": [list(range(world))],
                "tokenizerProcessNumber": 8,
                "multiNodesInferEnabled": False,
                "multiNodesInferPort": 1120,
                "interNodeTLSEnabled": False,
                "interNodeTlsCaPath": "security/grpc/ca/",
                "interNodeTlsCaFiles": ["ca.pem"],
                "interNodeTlsCert": "security/grpc/certs/server.pem",
                "interNodeTlsPk": "security/grpc/keys/server.key.pem",
                "interNodeTlsCrlPath": "security/grpc/certs/",
                "interNodeTlsCrlFiles": ["server_crl.pem"],
                "kvPoolConfig": {"backend": "", "configPath": ""},
                "ModelDeployConfig": {
                    "maxSeqLen": 8192,
                    "maxInputTokenLen": 7680,
                    "truncation": 0,
                    "ModelConfig": [
                        {
                            "modelInstanceType": "Standard",
                            "modelName": str(served),
                            "modelWeightPath": str(instance.model_path),
                            "worldSize": world,
                            "cpuMemSize": 0,
                            "npuMemSize": -1,
                            "backendType": "atb",
                            "trustRemoteCode": True,
                            "async_scheduler_wait_time": 120,
                            "kv_trans_timeout": 10,
                            "kv_link_timeout": 1080,
                            "models": {
                                "layerwiseDisaggregatedMasterDeviceNum": 2,
                                "layerwiseDisaggregatedSlaveDeviceNum": 8,
                            },
                        }
                    ],
                },
                "ScheduleConfig": {
                    "templateType": "Standard",
                    "templateName": "Standard_LLM",
                    "cacheBlockSize": 128,
                    "maxPrefillBatchSize": 50,
                    "maxPrefillTokens": 8192,
                    "prefillTimeMsPerReq": 150,
                    "prefillPolicyType": 0,
                    "decodeTimeMsPerReq": 50,
                    "decodePolicyType": 0,
                    "maxBatchSize": 200,
                    "maxIterTimes": 512,
                    "maxPreemptCount": 0,
                    "supportSelectBatch": False,
                    "maxQueueDelayMicroseconds": 5000,
                    "maxFirstTokenWaitTime": 2500,
                },
            },
            "LogConfig": {
                "dynamicLogLevel": "",
                "dynamicLogLevelValidHours": 2,
                "dynamicLogLevelValidTime": "",
            },
            "EnableDynamicAdjustTimeoutConfig": False,
        }
        return [_bootstrap_script(conf, str(instance.model_path)), "bash"]
