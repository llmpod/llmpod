"""MindIE 适配器单测。

MindIE 是文件驱动引擎（config.json + mindieservice_daemon），与 CLI 参数型引擎
（vllm/sglang）不同：generate_start_args 产出 bash 引导脚本 + $0 占位两个
positional token，容器 argv 为 `/bin/bash -c <脚本> bash <用户flags...>`。
"""

from __future__ import annotations

import asyncio
import json
import shlex

import pytest

from llmpod.apps.manager.deployment import image_catalog
from llmpod.apps.manager.deployment.command import (
    command_placeholder_names,
    render_command_template,
    validate_command_template,
)
from llmpod.apps.manager.deployment.engine import get_adapter, registered_engines
from llmpod.apps.manager.deployment.engine.mindie import MindIEAdapter
from llmpod.apps.manager.deployment.schemas import EngineConfig, InstanceSpec, Topology
from llmpod.core.exceptions import LLMPodValidationError


@pytest.fixture
def adapter():
    # get_adapter 注入 engine_name；裸实例化该键为空串
    return get_adapter("mindie")


def _conf_from_script(script: str) -> dict:
    """从引导脚本里抠出 config.json 并还原（heredoc 定界符 LLMPOD_CONF_EOF）。"""
    start = script.index("<<'LLMPOD_CONF_EOF'\n") + len("<<'LLMPOD_CONF_EOF'\n")
    end = script.index("\nLLMPOD_CONF_EOF", start)
    return json.loads(script[start:end])


class TestRegistry:
    def test_registered(self):
        assert "mindie" in registered_engines()

    def test_service_port_band_declared(self):
        """一个实例占业务+管理+指标三个相邻口——端口分配按此整组登记（消融改 1 即红）。"""
        assert MindIEAdapter.service_port_band == 3


class TestGenerateStartArgs:
    def test_single_gpu_config(self, adapter):
        """单卡：脚本内 config.json 各字段按 instance 渲染（结构对齐 3.1.0 出厂模板）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", served_model_name="qwen", port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        assert len(args) == 2  # [引导脚本, $0 占位]
        script, zero = args
        assert zero == "bash"
        assert "--config-file" in script  # daemon 只认 --config-file（310P 真机 strings 实证）
        conf = _conf_from_script(script)
        # 出厂模板顶层四段（310P 真机核验的结构，嵌套错误 daemon 拒启或静默空配）
        assert set(conf) == {
            "Version",
            "ServerConfig",
            "BackendConfig",
            "LogConfig",
            "EnableDynamicAdjustTimeoutConfig",
        }
        server = conf["ServerConfig"]
        assert server["port"] == 1025
        assert server["ipAddress"] == "0.0.0.0"
        assert server["httpsEnabled"] is False
        assert server["allowAllZeroIpListening"] is True
        assert server["managementPort"] == 1026
        assert server["metricsPort"] == 1027
        backend = conf["BackendConfig"]
        assert backend["npuDeviceIds"] == [[0]]
        assert "worldSize" not in backend  # worldSize 只在模型条目上（出厂模板同构）
        model = backend["ModelDeployConfig"]["ModelConfig"][0]
        assert model["modelWeightPath"] == "/models/Qwen"
        assert model["modelName"] == "qwen"
        assert model["worldSize"] == 1
        # 键名对齐出厂模板（310P 真机模板实证；此前误写 interNodePk 被 daemon 静默忽略）
        assert "interNodeTlsPk" in backend and "interNodePk" not in backend

    def test_bootstrap_patches_torch_dtype(self, adapter):
        """310P 不支持 bfloat16：引导脚本幂等改写模型 config.json 为 fp16（310P 真机实测）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", served_model_name="q", port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        script = args[0]
        assert 'sed -i \'s/"torch_dtype": *"bfloat16"/"torch_dtype": "float16"/\' /models/Qwen/config.json' in script
        # sed 行以 || true 收尾：模型卷意外只读时失败不阻断，daemon 以原始报错暴露
        sed_line = next(ln for ln in script.splitlines() if ln.startswith("sed -i"))
        assert sed_line.endswith("|| true")

    def test_bootstrap_quotes_model_path(self, adapter):
        """远端仓库文件名可携带 shell 元字符：拼进 bash -c 脚本体前必须 shell 引用。"""
        nasty = "/models/$(touch${IFS}/tmp/pwned)"
        args = adapter.generate_start_args(
            InstanceSpec(model_path=nasty, served_model_name="q", port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        sed_line = next(ln for ln in args[0].splitlines() if ln.startswith("sed -i"))
        # shlex 还原后路径是单个字面 token → bash 不会对其做命令替换
        assert shlex.split(sed_line)[3] == f"{nasty}/config.json"
        assert f" {nasty}/config.json " not in sed_line

    def test_bootstrap_repairs_clobbered_torch_path(self, adapter):
        """mindie-llm set_env.sh 用 `import torch` 探测 PYTORCH_INSTALL_PATH：设备被节点
        代理占用（DCMI -8020）时 CANN/driver 把错误日志写进 **stdout**，$(...) 连日志
        一起捕获成多行脏值（非空！空值兜底 ${VAR:-…} 拦不住）→ torch/lib 拼出非法目录，
        daemon 报 libtorch_cpu.so 缺失退出 127（NPU 节点实测）。脚本须在 source 链之后
        **强制覆写**为已核验路径并把 torch/lib 注入 LD_LIBRARY_PATH。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", served_model_name="q", port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        script = args[0]
        assert "PYTORCH_INSTALL_PATH=/usr/local/lib64/python3.11/site-packages/torch" in script
        assert 'export LD_LIBRARY_PATH="$PYTORCH_INSTALL_PATH/lib' in script
        # 覆写必须在 source 链之后（否则又被探测脏值盖回去）
        assert script.index("source /usr/local/Ascend/mindie/set_env.sh") < script.index("PYTORCH_INSTALL_PATH=")

    def test_daemon_not_pid1(self, adapter):
        """daemon 不能做容器 PID 1：PID 1 是 session leader，daemon 启动期 setpgid(0,0)
        必 EPERM（MIE04E01011C）→ 容器 ExitCode 255（NPU 节点实测）。脚本须以「后台
        拉起 + wait + 退出码透传」形态收尾，且绝不能出现 exec daemon。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", served_model_name="q", port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        script = args[0]
        lines = script.splitlines()
        assert not any(ln.startswith("exec ") for ln in lines)  # exec 会把 daemon 变成 PID 1
        daemon_line = next(ln for ln in lines if "mindieservice_daemon" in ln)
        assert daemon_line.rstrip().endswith("&")  # 后台子进程
        assert any(ln.startswith("wait ") for ln in lines)  # 父 bash 收尸、持容器存活
        # 退出码透传：daemon 崩溃时容器退出码=daemon 退出码（readiness 判死活靠它）
        assert lines[-1] == 'exit "$status"'

    def test_port_defaults_to_1025(self, adapter):
        """port=None：回落 MindIE 出厂业务端口 1025（reconciler 端口单源）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=None, nnodes=1),
            Topology(tp=1),
            {},
        )
        server = _conf_from_script(args[0])["ServerConfig"]
        assert server["port"] == 1025
        assert server["managementPort"] == 1026
        assert server["metricsPort"] == 1027

    def test_multi_gpu_logical_renumber(self, adapter):
        """多卡：npuDeviceIds 用逻辑重编号 0..N-1（物理选卡由设备直通承担）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=1030,
                nnodes=1,
                assigned_gpus=[2, 3],
            ),
            Topology(tp=2),
            {},
        )
        conf = _conf_from_script(args[0])
        backend = conf["BackendConfig"]
        assert backend["npuDeviceIds"] == [[0, 1]]
        model = backend["ModelDeployConfig"]["ModelConfig"][0]
        assert model["worldSize"] == 2

    def test_heredoc_special_chars_survive(self, adapter):
        """模型名含 ``"``/``$``/反引号：heredoc 引号定界符零展开，json.loads 仍可还原。"""
        nasty = 'Qwen"$(rm -rf /)`id`$HOME'
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name=nasty, port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        conf = _conf_from_script(args[0])
        assert conf["BackendConfig"]["ModelDeployConfig"]["ModelConfig"][0]["modelName"] == nasty


class TestAssembleArgs:
    """三层合并后保持 ``bash -c`` argv 形态：[脚本, $0 占位, 用户flags...]。"""

    def test_script_and_zero_placeholder_first(self, adapter):
        entry = image_catalog.ImageEntry(id="t", engine="mindie", arch="cann", image="x")
        args = adapter.assemble_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=1025, nnodes=1),
            Topology(tp=1),
            entry,
            {"extra_args": ["--some-flag", "v"]},
        )
        assert len(args) >= 3
        assert args[0].startswith("set -e\n")  # 脚本体在首位（bash -c 的脚本体）
        assert args[1] == "bash"  # $0 占位紧随（否则首个用户 flag 被 bash 吃掉）
        i = args.index("--some-flag")
        assert args[i + 1] == "v"
        assert i > 1  # 用户 flag 落在 "$@" 区（尾部）

    def test_protected_config_flag_dropped(self, adapter):
        entry = image_catalog.ImageEntry(id="t", engine="mindie", arch="cann", image="x")
        args = adapter.assemble_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=1025, nnodes=1),
            Topology(tp=1),
            entry,
            {"extra_args": ["--config-file", "/evil.json", "--ok"]},
        )
        assert "--config-file" not in args and "/evil.json" not in args
        assert "--ok" in args


class TestCommandMode:
    """完整启动命令模式：模板可回传、经镜像入口执行、渲染回真实实例后与自动生成等价。"""

    def _inst(self, **kw) -> InstanceSpec:
        base = {
            "model_path": "/data/models/qwen2-5-1-5b",
            "served_model_name": "qwen",
            "gpu_arch": "cann",
            "port": 18234,
            "tp": 2,
            "pp": 1,
            "nnodes": 1,
            "assigned_gpus": [0, 1],
        }
        base.update(kw)
        return InstanceSpec(**base)

    def _entry(self):
        return image_catalog.ImageEntry(id="t", engine="mindie", arch="cann", image="x")

    def test_template_has_all_placeholders_and_validates(self, adapter):
        inst = self._inst()
        tpl = adapter.command_template(adapter.assemble_args(inst, Topology(tp=2), self._entry(), {}), inst)
        names = command_placeholder_names(tpl)
        assert {"port", "management_port", "metrics_port", "device_ids"} <= set(names)
        assert "{model_path}" in tpl and "{model_path_json}" in tpl
        assert "{served_model_name_json}" in tpl
        validate_command_template(tpl, has_multinode=False, required=[])

    def test_template_does_not_tail_zero_placeholder_or_flags(self, adapter):
        """模板只有脚本本体：``bash`` $0 占位与 extra_args 落在 exit 之后永远不执行。"""
        inst = self._inst()
        args = adapter.assemble_args(inst, Topology(tp=2), self._entry(), {"extra_args": ["--max-batch-size", "32"]})
        tpl = adapter.command_template(args, inst)
        assert "--max-batch-size" not in tpl
        assert not tpl.endswith(" bash")

    def test_template_renders_back_to_generated_script(self, adapter):
        """模板渲染回真实实例 → 与自动生成的 config.json 逐字段一致（含卡数不等 tp 的异常态）。"""
        for gpus in ([0, 1], [5]):  # 后者：卡数与 tp 不等，模板与自动生成仍须同源
            inst = self._inst(assigned_gpus=gpus)
            auto = adapter.assemble_args(inst, Topology(tp=2), self._entry(), {})
            replayed = _conf_from_script(render_command_template(adapter.command_template(auto, inst), inst, adapter))
            replayed_bc = replayed["BackendConfig"]
            auto_bc = _conf_from_script(auto[0])["BackendConfig"]
            assert replayed_bc["npuDeviceIds"] == auto_bc["npuDeviceIds"]
            assert (
                replayed_bc["ModelDeployConfig"]["ModelConfig"][0]["worldSize"]
                == auto_bc["ModelDeployConfig"]["ModelConfig"][0]["worldSize"]
            )
            server = replayed["ServerConfig"]
            assert (server["port"], server["managementPort"], server["metricsPort"]) == (18234, 18235, 18236)
            assert replayed_bc["ModelDeployConfig"]["ModelConfig"][0]["modelWeightPath"] == inst.model_path

    def test_run_argv_keeps_image_entrypoint(self, adapter):
        """命令模式保住镜像入口：覆盖它会让 CANN 环境初始化丢失（Exit127）。"""
        entrypoint, args = adapter.command_run_argv("echo hi | tee /tmp/x", "cann")
        assert entrypoint == adapter.entrypoint
        assert args == ["echo hi | tee /tmp/x", "bash"]

    def test_template_detects_script_shape_drift(self, adapter):
        """脚本形态变了就抛错，不能把占位值静默固化进模板。"""
        with pytest.raises(LLMPodValidationError):
            adapter.command_template(["set -e\necho hi\n", "bash"], self._inst())

    def test_served_name_falls_back_to_model_path(self, adapter):
        """served_model_name 为空时回填模型路径（否则 JSON 缺键，ConfigManager 崩）。"""
        inst = self._inst(served_model_name=None)
        script = adapter.assemble_args(inst, Topology(tp=2), self._entry(), {})[0]
        tpl = adapter.command_template([script, "bash"], inst)
        rendered = render_command_template(tpl, inst, adapter)
        conf = _conf_from_script(rendered)["BackendConfig"]
        assert conf["ModelDeployConfig"]["ModelConfig"][0]["modelName"] == inst.model_path


class TestTopologyConflict:
    def test_multinode_rejected(self, adapter):
        """nnodes>1 → 冲突文案（自动回落序据此剔除 cann）。"""
        conflict = adapter.topology_conflict("cann", Topology(tp=1, nnodes=2))
        assert conflict is not None and "多机" in conflict

    def test_pp_rejected(self, adapter):
        conflict = adapter.topology_conflict("cann", Topology(tp=1, pp=2))
        assert conflict is not None and "pp" in conflict

    def test_single_node_tp_ok(self, adapter):
        """单机 tp>1 不冲突（worldSize=npu 数表达）。"""
        assert adapter.topology_conflict("cann", Topology(tp=2, pp=1, nnodes=1)) is None

    def test_non_cann_no_opinion(self, adapter):
        """非 cann 架构不表态（供混布回落序共用）。"""
        assert adapter.topology_conflict("nvidia", Topology(tp=1, pp=2)) is None

    def test_pp_instance_raises_on_generate(self, adapter):
        """pp>1 实例在组装期硬拒（三重保险的最后一道）。"""
        with pytest.raises(LLMPodValidationError, match="pp"):
            adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=1025, nnodes=1, pp=2),
                Topology(tp=1, pp=2),
                {},
            )

    def test_multinode_instance_raises_on_generate(self, adapter):
        with pytest.raises(LLMPodValidationError, match="多机"):
            adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=1025, nnodes=2),
                Topology(tp=1, nnodes=2),
                {},
            )


class TestBuildDeviceConfig:
    def test_cann(self):
        dc = MindIEAdapter.build_device_config(InstanceSpec(assigned_gpus=[0]), "cann")
        assert dc.runtime == "ascend"
        assert dc.gpus is None  # NPU 走 runtime + extra_devices，不走 nvidia device_requests
        assert any("/dev/davinci0" in d for d in dc.extra_devices)
        # 管理设备三件套（manager/devmm_svm/hisi_hdc）不可缺
        for dev in ("/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"):
            assert any(d.startswith(dev) for d in dc.extra_devices), dev
        assert dc.privileged is False
        # 宿主 driver/dcmi 只读挂载五条
        assert len(dc.driver_mounts) == 5

    def test_multi_npu_devices(self):
        dc = MindIEAdapter.build_device_config(InstanceSpec(assigned_gpus=[2, 3]), "cann")
        assert any("/dev/davinci2" in d for d in dc.extra_devices)
        assert any("/dev/davinci3" in d for d in dc.extra_devices)

    def test_unsupported_arch(self):
        with pytest.raises(NotImplementedError):
            MindIEAdapter.build_device_config(InstanceSpec(), "nvidia")


class TestBuildEnv:
    def test_visible_devices_comma_joined(self, adapter):
        env = adapter.build_env(
            InstanceSpec(nnodes=1, gpu_arch="cann", assigned_gpus=[1, 3]),
            Topology(tp=2),
            EngineConfig(),
        )
        assert env["ASCEND_VISIBLE_DEVICES"] == "1,3"
        assert env["ASCEND_RT_VISIBLE_DEVICES"] == "1,3"

    def test_no_gpus_defaults_first(self, adapter):
        env = adapter.build_env(
            InstanceSpec(nnodes=1, gpu_arch="cann"),
            Topology(),
            EngineConfig(),
        )
        assert env["ASCEND_VISIBLE_DEVICES"] == "0"

    def test_no_pytorch_alloc_conf(self, adapter):
        """MindIE 非 torch 分配器：不注入 PYTORCH_NPU_ALLOC_CONF（vllm cann 分支才有）。"""
        env = adapter.build_env(
            InstanceSpec(nnodes=1, gpu_arch="cann"),
            Topology(),
            EngineConfig(),
        )
        assert "PYTORCH_NPU_ALLOC_CONF" not in env

    def test_no_slog_stdout_poison(self, adapter):
        """绝不注入 ASCEND_SLOG_PRINT_TO_STDOUT：它让 libascend_hal.so 往 stdout 写驱动
        日志，污染 ATB set_env.sh 的 ABI 探测 `$(...)`，误加载 cxx_abi_0 版 libatb.so
        → warmup 建图 SIGSEGV（容器 134，NPU 节点 A/B 实验实锤，Qwen3-4B/Qwen2.5-1.5B
        双模型复现）。CANN 日志走默认文件路径（容器可写层），排障能力不降。"""
        env = adapter.build_env(
            InstanceSpec(nnodes=1, gpu_arch="cann"),
            Topology(),
            EngineConfig(),
        )
        assert "ASCEND_SLOG_PRINT_TO_STDOUT" not in env

    def test_bootstrap_pins_atb_cxx_abi(self, adapter):
        """ATB 层 source 必须带 --cxx_abi=1：set_env.sh 的 get_cxx_abi_option() 用
        `$(python3 -c 'import torch; ...') == "True"` 判 ABI，stdout 被任何日志污染即
        误判 ABI=0 → 加载 cxx_abi_0 版 libatb.so，与 ABI1 编译的消费者混用，warmup
        建图段错误（容器 134，NPU 节点实测）。--cxx_abi=1 让脚本跳过 torch 探测。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=1025, nnodes=1),
            Topology(tp=1),
            {},
        )
        script = args[0]
        assert "source /usr/local/Ascend/nnal/atb/set_env.sh --cxx_abi=1 2>/dev/null || true" in script


class TestHealthCheck:
    def test_ready_path_and_predicate(self, adapter):
        path, pred = adapter.health_check()
        # 业务口唯一可探路径（310P 真机实测：/health 等恒 404，权威探针在管理口够不着）
        assert path == "/v1/models"

        class _Resp:
            def __init__(self, code):
                self.status_code = code

        assert pred(_Resp(200)) is True
        # 业务口 404（路由不存在/未监听阶段）不算就绪
        assert pred(_Resp(404)) is False
        assert pred(_Resp(500)) is False


class TestEstimateVramGb:
    def test_default_formula(self, adapter):
        """不覆写 base 口径：per-gpu = (权重 + KV)/并行度（与 FP16 落盘一致）。"""
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1)) == 10.0
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1, max_model_len=8192)) == 11.0


class TestImageCatalog:
    def test_catalog_entry_matches_310p(self):
        """mindie-ascend-310p 条目五维匹配 310P3 卡（engine/arch/gpu_model/driver）。

        gpu_model 只放行已实测的 *310P*（fail-closed）：300I Duo 与 310P 同芯异名，
        但平台上未见该命名上报的卡，放行未实测卡型与适配器「只声明已验证的」哲学
        相悖——实测有此类卡后再扩。
        """
        image_catalog.reload()
        entries = image_catalog.list_entries(engine="mindie", arch="cann", gpu_model="310P3", driver_version="25.2.3")
        assert any(e.gpu_model == ["*310P*"] for e in entries)
        # 条目 params.args 必须为空列表：image 层 positional 会渲染进 "$@" 传给 daemon
        for e in entries:
            assert (e.params.get("args") or []) == []

    def test_schedulable_archs_keeps_cann(self, adapter):
        """镜像目录命中时 cann 留在可调度架构（混布回落序的入口判据）。"""

        class _Card:
            node_id = "n1"
            index = 0
            status = "online"
            arch = "cann"
            name = "310P3"
            driver_version = "25.2.3"

            def __init__(self):
                self.uuid = "c1"

        archs = adapter.schedulable_archs(gpu_pool=[_Card()], engine_version=None)
        assert "cann" in archs


class TestCapabilityExposure:
    def test_engines_route_lists_mindie(self):
        """GET /admin/inference-images/engines 冒烟：mindie 装配不异常（防被静默吞）。"""
        from llmpod.apps.webserver.routes.admin.inference_image import list_engines

        payload = asyncio.run(list_engines(model_id=None, db=None))
        by_engine = {item.engine: item for item in payload["data"]}
        assert "mindie" in by_engine
        item = by_engine["mindie"]
        assert item.supported_gpu_archs == ["cann"]
        assert item.supports_multinode is False
        assert item.deployable is True
        assert item.protected_flags == ["--config-file"]

    def test_capability_class_attrs(self):
        """类属性形态：单机单 LLM chat、无并行 flag、host 网络、1025 端口。"""
        assert MindIEAdapter.supported_gpu_archs == ["cann"]
        assert MindIEAdapter.supports_multinode is False
        assert MindIEAdapter.supported_parallel == []
        assert MindIEAdapter.entrypoint == ["/usr/local/bin/docker-entrypoint.sh", "/bin/bash", "-c"]
        assert MindIEAdapter.default_port == 1025
        assert MindIEAdapter.default_network_mode == "host"
        assert MindIEAdapter.protected_flags == ["--config-file"]
        assert MindIEAdapter.param_suggestions == []
        assert MindIEAdapter.memory_utilization_flag == ""
