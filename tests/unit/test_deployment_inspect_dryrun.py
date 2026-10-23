"""inspect dry-run 三层解析单测：直调编排服务的 _dry_run_resolve（真实预检路径）。

不起 HTTP / 不查 DB：手搭 InspectState（基线经 _prepare_baseline 装配，arch/驱动按
场景置入）后直调 dry-run 阶段函数，验三层覆盖 + protected 免疫 + 多机占位 + 镜像
降级。此前该文件复刻路由逻辑（埋在路由里没法 import），编排迁出后改为直调真函数，
复刻副本消灭。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.apps.webserver.services import deployment_inspect as inspect_svc
from llmpod.apps.webserver.services.deployment_inspect import InspectState
from llmpod.models.model import Model


def _dry_run(
    arch="nvidia",
    *,
    engine_params=None,
    image_tag=None,
    nnodes=1,
    tp=1,
    pp=1,
    model_path=None,
    driver_version=None,
    gpu_model=None,
):
    """驱动 dry-run 阶段（真实路径），返回填充后的 InspectState（断言读 c.resolved / c.issues）。

    arch / driver_version / gpu_model 对应 _resolve_target_arch 的产物（手选卡路径从
    所选卡取）；未给时按自动路径默认。镜像解析与预检同口径：抛错降级 image=None +
    一条 issue，不阻断 args/env 产出。
    """
    model = Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors")
    request = InspectDeploymentRequest(
        model_id=1,
        cluster_id=1,
        engine="vllm",
        topology=Topology(tp=tp, pp=pp, nnodes=nnodes),
        engine_params=engine_params or {},
        image_tag=image_tag,
        model_path=model_path,
    )
    c = InspectState(request=request, model=model)
    inspect_svc._prepare_baseline(c)
    c.arch = arch
    c.target_driver = driver_version
    c.target_gpu_model = gpu_model
    c.cpu_only = False
    inspect_svc._dry_run_resolve(c)
    return c


class TestResolvedDryRun:
    def test_system_args_present(self):
        """系统层 flag（--model/--port/--tensor-parallel-size）在 resolved.args 里。"""
        c = _dry_run(tp=2)
        args = c.resolved.args
        assert "--model" in args and "<model-1>" in args
        assert "--tensor-parallel-size" in args
        assert "2" in args

    def test_user_extra_args_merged(self):
        """用户 extra_args 非保护 flag 被合并进 resolved.args。"""
        ep = {"extra_args": ["--trust-remote-code", "--dtype", "half"]}
        c = _dry_run(engine_params=ep)
        args = c.resolved.args
        assert "--trust-remote-code" in args
        assert "--dtype" in args and "half" in args

    def test_protected_flag_immune_to_user(self):
        """protected flag（--model）用户传了也被忽略，保系统值。"""
        ep = {"extra_args": ["--model", "user-override"]}
        c = _dry_run(engine_params=ep)
        args = c.resolved.args
        # --model 仍是系统值 <model-1>，不是 user-override
        idx = args.index("--model")
        assert args[idx + 1] == "<model-1>"
        # overlap 报出
        assert "--model" in c.resolved.protected_overlap

    def test_protected_flag_immune_to_user_inline_form(self):
        """连写 ``--flag=value`` 与分写同名同免疫：argparse 后写生效，若把它当新参数
        追加到末尾，用户值反而会覆盖系统真值（tp=2 卡上起 4 路 TP → 起不来/OOM）。"""
        ep = {"extra_args": ["--tensor-parallel-size=4", "--model=user.gguf"]}
        c = _dry_run(tp=2, engine_params=ep)
        args = c.resolved.args
        # 系统值仍在，且命令行里不再出现任何 tensor-parallel-size=4 / user.gguf
        idx = args.index("--tensor-parallel-size")
        assert args[idx + 1] == "2"
        assert not any(str(a).startswith("--tensor-parallel-size=") for a in args)
        idx = args.index("--model")
        assert args[idx + 1] == "<model-1>"
        assert "--model=user.gguf" not in args
        # 连写形式同样进 overlap 告警
        overlap = c.resolved.protected_overlap
        assert "--tensor-parallel-size=4" in overlap and "--model=user.gguf" in overlap

    def test_user_inline_form_overrides_nonprotected(self):
        """非 protected flag 的连写覆盖生效（与分写同语义：替换系统值，不追加重复 flag）。

        渲染统一分写形态（``--dtype half``）——argparse 与连写等价，不保留输入侧写法。
        """
        ep = {"extra_args": ["--dtype=half"]}
        c = _dry_run(engine_params=ep)
        args = c.resolved.args
        idx = args.index("--dtype")
        assert args[idx + 1] == "half"  # 覆盖了系统默认值，且无重复 --dtype
        assert args.count("--dtype") == 1

    def test_user_env_overrides_system(self):
        """resolved.env 只回显用户自定义项：引擎层不再回显（assemble_env 仍会合并，
        但 inspect 出口只透传用户 env）——前端预填回传不会把系统注入值固化成用户层。"""
        ep = {"env": {"HF_HUB_OFFLINE": "0", "CUSTOM": "x"}}
        c = _dry_run(engine_params=ep)
        assert c.resolved.env == {"HF_HUB_OFFLINE": "0", "CUSTOM": "x"}

    def test_resolved_env_excludes_system_injections(self):
        """系统注入键（HF_*/SOCKET_IFNAME/READY_TIMEOUT）不进 resolved.env，
        即使用户 env 里同键覆盖了它们（覆盖在真实下发时仍生效，只是不回显）。"""
        ep = {"env": {"HF_HUB_OFFLINE": "0", "CUSTOM": "x"}}
        c = _dry_run(engine_params=ep, nnodes=2, tp=2)
        env = c.resolved.env
        assert "HF_ENDPOINT" not in env
        assert "VLLM_ENGINE_READY_TIMEOUT_S" not in env
        assert "GLOO_SOCKET_IFNAME" not in env
        assert "NCCL_SOCKET_IFNAME" not in env

    def test_resolved_env_empty_without_user_env(self):
        """不传用户 env → resolved.env 为空（不再回显系统层默认值）。"""
        c = _dry_run()
        assert c.resolved.env == {}

    def test_multi_node_rendezvous_placeholder(self):
        """多机 nnodes>1 → resolved.args 含占位 --master-addr + --node-rank + --master-port，
        并给「占位值」说明 warning（部署期定稿，不阻断）。"""
        c = _dry_run(nnodes=2, tp=2)
        args = c.resolved.args
        assert "--master-addr" in args and "<master-addr>" in args
        assert "--node-rank" in args
        assert "--master-port" in args
        assert "--nnodes" in args
        assert any("占位值" in w for w in c.warnings)
        assert c.issues == []

    def test_multi_node_pp_placeholder(self):
        """多机 pp 形态（tp=1/pp=2/nnodes=2）→ resolved.args 含 --pipeline-parallel-size 2
        （占位 InstanceSpec 补了 pp，预览命令与将提交的形状一致）。"""
        c = _dry_run(nnodes=2, tp=1, pp=2)
        args = c.resolved.args
        assert "--pipeline-parallel-size" in args
        assert args[args.index("--pipeline-parallel-size") + 1] == "2"
        assert "--master-addr" in args  # 多机占位同在

    def test_single_node_no_rendezvous(self):
        """单机 nnodes=1 → 无 --master-addr 等多机 flag、无占位提醒。"""
        c = _dry_run(nnodes=1, tp=1)
        assert "--master-addr" not in c.resolved.args
        assert "--nnodes" not in c.resolved.args
        assert not any("占位" in w for w in c.warnings)

    def test_vram_percent_injects_gpu_mem_util(self):
        """系统层 util 注入链：vram_percent（= c.util）→ args 含 --gpu-memory-utilization。

        不带用户 extra_args——用户自带该 flag 时断言会被用户层合并满足，锁不住系统层
        注入（丢掉 vram_percent=c.util 仍全绿）。
        """
        c = _dry_run(tp=1)
        args = c.resolved.args
        assert "--gpu-memory-utilization" in args
        assert c.resolved.gpu_memory_utilization == 0.9

    def test_user_vram_percent_echoed(self):
        """用户显存占比 flag（引擎唯一定义处）→ resolved 回显生效值 + args 注入 flag。"""
        ep = {"extra_args": ["--gpu-memory-utilization", "0.5"]}
        c = _dry_run(engine_params=ep)
        args = c.resolved.args
        assert "--gpu-memory-utilization" in args
        assert "0.5" in args
        assert c.resolved.gpu_memory_utilization == 0.5

    def test_cann_arch_uses_ascend_args(self):
        """arch=cann → resolved.args 走 _ascend_args（vllm serve 前缀 + --dtype half）。"""
        c = _dry_run(arch="cann")
        args = c.resolved.args
        assert "vllm" in args and "serve" in args
        assert "--dtype" in args and "half" in args

    def test_image_incompatible_driver_degrades(self):
        """驱动不满足 image_tag 约束 → 降级 image=None + 一条 issue，args 仍产出（不阻断预览）。

        复刻用户场景：vllm/nvidia@0.26.0 镜像要求 driver >=575,<580；卡驱动 550.54 不满足。
        预检捕获后降级 resolved.image=None + issue（含驱动要求），而非整体 422。
        """
        c = _dry_run(image_tag="0.26.0", driver_version="550.54")
        assert c.resolved.image is None  # 降级：未解析出镜像
        assert "--model" in c.resolved.args  # system 层 args 仍产出，预览不中断
        assert c.deployable is False
        assert any("0.26.0" in i for i in c.issues)

    def test_image_compatible_driver_resolves(self):
        """对照：驱动满足约束（576.40 ∈ >=575,<580）→ 正常解析出镜像。"""
        c = _dry_run(image_tag="0.26.0", driver_version="576.40")
        assert c.resolved.image is not None
        assert "0.26.0" in c.resolved.image


class TestMergeStartArgsUnits:
    """参数单元推进回归：skip/覆盖必须按完整单元消费 token，不多不少。"""

    def test_protected_split_form_does_not_swallow_neighbor(self):
        """skip 命中分写 flag 时只跳它和它的值——不得连后面的无关 flag 一起吞掉。

        回归：旧实现的 skip 分支在分写路径上多推进一格，用户跟在被忽略参数后面的
        合法参数（如 --trust-remote-code）被静默丢弃。
        """
        from llmpod.apps.manager.deployment.engine.base import merge_start_args

        merged = merge_start_args(
            [], ["--model", "sys"], ["--model", "x", "--trust-remote-code"], protected={"--model"}
        )
        assert merged == ["--model", "sys", "--trust-remote-code"]

    def test_image_inline_flag_skipped_without_hang(self):
        """镜像层连写 flag 与系统层同名 → 整单元跳过且必须终止。

        回归：旧实现在连写 skip 路径上不推进游标，同一 token 无限重试（部署路径挂死）。
        顺带验维度匹配本身：``--model=img.gguf`` 与 ``--model sys`` 同名（连写 ≡ 分写）。
        """
        from llmpod.apps.manager.deployment.engine.base import merge_start_args

        # 参数序 = (image 层, system 层, user 层)：系统层已设 --model，镜像层同名连写被跳过
        merged = merge_start_args(["--model=img.gguf", "--port=8000"], ["--model", "sys"], [])
        assert merged == ["--model", "sys", "--port", "8000"]  # 渲染统一分写形态
