"""inspect 请求渐进三阶段 / 集群推荐 / 引擎参数回显的单测（FakeSession 直调路由）。

三阶段契约：仅 model_id → 推荐引擎/拓扑（无校验无 dry-run）；+engine → 仍无 dry-run；
+engine+topology → 结构校验 + resolved 三层解析回显。集群推荐：cluster_id 不填时遍历
active 集群按「单节点可行优先、跨机次之」挑选。本文件与 test_inspect_occupancy.py 等
一起构成 inspect 编排迁移的行为锁。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import EngineConfig, GpuSelector, Topology
from llmpod.apps.webserver.routes.admin import deployment as route_mod
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.core.exceptions import LLMPodValidationError, NotFound
from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.node import Node
from tests.unit._fakes import FakeSession


def _node(nid, ip="10.0.0.1", cluster=1, status="online", mem=64.0):
    return Node(id=nid, hostname=nid, ip=ip, cluster_id=cluster, status=status, memory_gb=mem)


def _gpu(i, uuid, node="n1", *, total=22.0, status="online", arch="nvidia"):
    return Gpu(
        id=i,
        uuid=uuid,
        node_id=node,
        index=i,
        name="NVIDIA GeForce RTX 2080 Ti",
        arch=arch,
        driver_version="595.84",
        total_vram_gb=total,
        status=status,
        spec={},
    )


def _seed(model, *objs):
    s = FakeSession()
    s.preload(model)
    for o in objs:
        s.preload(o)
    return s


def _inspect(session, **kw):
    base = dict(model_id=1, cluster_id=1, engine="vllm", topology=Topology(tp=1, max_model_len=8192))
    base.update(kw)
    return route_mod.inspect_deployment(InspectDeploymentRequest(**base), session)


_M = Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors")
_M_BIG = Model(id=1, name="m", model_type="llm", size_gb=30.0, format="safetensors")


class TestStageProgression:
    """按请求完整度三阶段递进：推荐 → 推荐集群评估 → 校验 + dry-run。"""

    async def test_model_only_returns_recommendation_without_dryrun(self):
        """阶段 1（仅 model_id）：有推荐、无校验副作用（resolved / placement 均空）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1), s)
        d = result["data"]
        assert d["deployable"] is True
        assert d["issues"] == []
        assert d["resolved"] is None
        assert d["recommended_placement"] is None
        assert d["recommendation"]["engine"] == "vllm"
        assert d["recommendation"]["topology"]["tp"] == 1

    async def test_engine_without_topology_still_no_dryrun(self):
        """阶段 2（+engine 无 topology）：沿用推荐拓扑，仍不产 resolved（须 engine+topology 齐）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1, engine="vllm"), s)
        assert result["data"]["resolved"] is None
        assert result["data"]["recommendation"]["engine"] == "vllm"

    async def test_cluster_without_engine_returns_pool_and_placement(self):
        """阶段 1.5（+cluster_id 无 engine）：卡池照常装配、推荐调度照给，resolved 仍空。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine=None, topology=None)
        d = result["data"]
        assert d["resolved"] is None
        assert len(d["cluster_gpus"]) == 1
        assert d["recommended_placement"]["gpu_uuids"] == ["g1"]

    async def test_full_stage_produces_resolved(self):
        """阶段 3（+engine+topology）：三层解析回显（镜像 / 参数 / 环境变量）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s)
        resolved = result["data"]["resolved"]
        assert resolved is not None
        assert resolved["image"] == "vllm/vllm-openai:v0.23.0"
        assert resolved["args"] == [
            "--model",
            "<model-1>",
            "--served-model-name",
            "<model-1>",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--tensor-parallel-size",
            "1",
            "--max-model-len",
            "8192",
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            "hermes",
            "--gpu-memory-utilization",
            "0.9",
        ]
        assert resolved["env"] == {}  # 系统注入键不回显，env 只透传用户自定义项
        assert resolved["protected_overlap"] == []
        assert resolved["gpu_memory_utilization"] == 0.9

    async def test_unknown_model_raises_not_found(self):
        s = _seed(_M)
        with pytest.raises(NotFound):
            await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=99), s)

    async def test_unknown_engine_rejected(self):
        """未知引擎 → LLMPodValidationError（HTTP 422），不静默回退默认引擎。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        with pytest.raises(LLMPodValidationError, match="不支持的引擎: bogus"):
            await _inspect(s, engine="bogus")


class TestEngineRecommendation:
    """引擎推荐：按 model_type / format 匹配注册表首个支持的引擎。"""

    async def test_gguf_recommends_llamacpp(self):
        m = Model(id=1, name="m", model_type="llm", size_gb=1.0, format="gguf")
        s = _seed(m, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1), s)
        assert result["data"]["recommendation"]["engine"] == "llamacpp"

    async def test_safetensors_recommends_vllm(self):
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1), s)
        assert result["data"]["recommendation"]["engine"] == "vllm"

    async def test_embedding_model_has_no_kv(self):
        """embedding 类别无自回归 KV cache → 推荐显存 = 权重大小（不加 KV 项）。"""
        m = Model(id=1, name="m", model_type="embedding", size_gb=1.0, format="safetensors")
        s = _seed(m, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1), s)
        assert result["data"]["recommendation"]["engine"] == "vllm"
        assert result["data"]["recommendation"]["vram_per_gpu_gb"] == 1.0


class TestVramEstimationInputs:
    """推荐显存的输入口径：KV 随 ctx / 精度档 / 自带上限变化。"""

    async def test_vram_includes_kv_at_default_context(self):
        """未传 max_model_len → 按 KV_BASE_CONTEXT(8192) 兜底：1GB 权重 + 0.1 KV。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1), s)
        assert result["data"]["recommendation"]["vram_per_gpu_gb"] == 1.1

    async def test_kv_dtype_fp8_halves_cache(self):
        """KV 精度档 fp8_e4m3（1 字节）→ KV 项减半：10GB + 1GB KV → 11GB（f16 口径 10GB+0.5GB）。"""
        m = Model(
            id=1,
            name="m",
            model_type="llm",
            size_gb=10.0,
            format="safetensors",
            num_layers=32,
            num_kv_heads=8,
            head_dim=128,
        )
        s = _seed(m, _node("n1"), _gpu(1, "g1"))
        base = await _inspect(s)
        fp8 = await _inspect(s, engine_params={"extra_args": ["--kv-cache-dtype", "fp8_e4m3"]})
        assert base["data"]["recommendation"]["vram_per_gpu_gb"] == 11.0
        assert fp8["data"]["recommendation"]["vram_per_gpu_gb"] == 10.5

    async def test_max_context_clamps_recommendation(self):
        """模型自带上限 4096 → 推荐 ctx 钳到 4096（KV 项随之变小），并给超限提示。"""
        m = Model(id=1, name="m", model_type="llm", size_gb=10.0, format="safetensors", max_context=4096)
        s = _seed(m, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s)
        d = result["data"]
        assert d["recommendation"]["topology"]["max_model_len"] == 4096
        assert d["recommendation"]["vram_per_gpu_gb"] == 10.5
        assert any("超过模型自带上下文上限 4096" in w for w in d["warnings"])


class TestClusterRecommendation:
    """cluster_id 不填 → 遍历 active 集群推荐：单节点可行优先，其次跨机，不可行 → None。"""

    def _clusters(self):
        return (
            Cluster(id=1, name="c1", type="docker", status="active", spec={}),
            Cluster(id=2, name="c2", type="docker", status="active", spec={}),
            Cluster(id=3, name="c3", type="docker", status="inactive", spec={}),
        )

    async def test_single_node_feasible_cluster_preferred(self):
        """c1 两张 22G（tp=2 可行）+ c2 一张 80G → 选 c1，且 tp 推导为 2、显存按 tp=2 口径。"""
        c1, c2, _ = self._clusters()
        s = _seed(
            _M_BIG,
            c1,
            c2,
            _node("n1", cluster=1),
            _node("n2", cluster=2),
            _gpu(1, "g1", "n1"),
            _gpu(2, "g2", "n1"),
            _gpu(3, "big1", "n2", total=80.0),
        )
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1, engine="vllm"), s)
        d = result["data"]
        assert d["recommended_cluster_id"] == 1
        assert d["cross_node"] is False
        assert d["recommendation"]["topology"]["tp"] == 2
        assert d["recommendation"]["vram_per_gpu_gb"] == 16.5

    async def test_falls_back_to_feasible_cluster(self):
        """c1 单张 22G（30GB 模型装不下）→ 落到 c2 的 80G 单卡（tp=1）。"""
        c1, c2, _ = self._clusters()
        s = _seed(
            _M_BIG,
            c1,
            c2,
            _node("n1", cluster=1),
            _node("n2", cluster=2),
            _gpu(1, "g1", "n1"),
            _gpu(3, "big1", "n2", total=80.0),
        )
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1, engine="vllm"), s)
        d = result["data"]
        assert d["recommended_cluster_id"] == 2
        assert d["recommendation"]["topology"]["tp"] == 1
        assert d["recommended_placement"]["gpu_uuids"] == ["big1"]

    async def test_no_feasible_cluster_returns_none(self):
        """全部集群装不下 → recommended_cluster_id=None、卡池空、placement=None、不报错。"""
        c1 = Cluster(id=1, name="c1", type="docker", status="active", spec={})
        s = _seed(_M_BIG, c1, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1, engine="vllm"), s)
        d = result["data"]
        assert d["recommended_cluster_id"] is None
        assert d["cluster_gpus"] == []
        assert d["recommended_placement"] is None
        assert d["deployable"] is True  # 无集群属「无数据」而非「不可部署」

    async def test_inactive_cluster_excluded(self):
        """inactive 集群的大卡不参与推荐（只有它可行时 → None，不选它）。"""
        c3 = Cluster(id=3, name="c3", type="docker", status="inactive", spec={})
        s = _seed(
            _M_BIG,
            c3,
            _node("n3", cluster=3),
            _gpu(3, "big1", "n3", total=80.0),
        )
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1, engine="vllm"), s)
        assert result["data"]["recommended_cluster_id"] is None

    async def test_cross_node_cluster_picked_when_single_node_infeasible(self):
        """显式 tp=2、每节点一张 22G 卡 ×2 节点：单节点凑不齐 → 跨机可行，cross_node=True。"""
        c1 = Cluster(id=1, name="c1", type="docker", status="active", spec={})
        s = _seed(
            _M,
            c1,
            _node("n1", cluster=1),
            _node("n2", cluster=1),
            _gpu(1, "g1", "n1"),
            _gpu(2, "g2", "n2"),
        )
        result = await route_mod.inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                engine="vllm",
                topology={"tp": 2, "max_model_len": 8192},  # dict 写法 → tp 记为显式
            ),
            s,
        )
        d = result["data"]
        assert d["recommended_cluster_id"] == 1
        assert d["cross_node"] is True

    async def test_all_ascend_cluster_picked_via_arch_fallback(self):
        """全 Ascend 平台（唯一 active 集群只有 cann 卡）：默认 arch=nvidia 无合格卡
        → 按适配器 arch_order 回落到 cann 再挑，自动推荐照常出（不再静默空推荐）。"""
        c1 = Cluster(id=1, name="c1", type="docker", status="active", spec={})
        s = _seed(
            _M,
            c1,
            _node("n1", cluster=1),
            Gpu(
                id=1,
                uuid="a1",
                node_id="n1",
                index=1,
                name="Ascend 310P3",
                arch="cann",
                driver_version="25.2.3",
                total_vram_gb=24.0,
                status="online",
                spec={},
            ),
        )
        result = await route_mod.inspect_deployment(InspectDeploymentRequest(model_id=1), s)
        d = result["data"]
        assert d["recommended_cluster_id"] == 1
        rec = d["recommended_placement"]
        assert rec is not None and rec["gpu_uuids"] == ["a1"]
        assert rec["arch"] == "cann"

    async def test_zero_cluster_id_treated_as_unset(self):
        """cluster_id=0（前端空输入 Number('') 常见产物）按未填写处理走自动推荐——
        否则会拼出「已指定集群却查不到卡」的全空绿灯响应。"""
        c1 = Cluster(id=1, name="c1", type="docker", status="active", spec={})
        s = _seed(_M, c1, _node("n1"), _gpu(1, "g1"))
        result = await route_mod.inspect_deployment(
            InspectDeploymentRequest(model_id=1, engine="vllm", cluster_id=0), s
        )
        assert result["data"]["recommended_cluster_id"] == 1


class TestEngineParamsEcho:
    """用户引擎参数（engine_params / engine_config / image / model_path）的回显与免疫。"""

    async def test_user_memory_utilization_echo(self):
        """用户显存占比 flag → resolved.gpu_memory_utilization 跟随（不强制引擎默认 0.9）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine_params={"extra_args": ["--gpu-memory-utilization", "0.5"]})
        assert result["data"]["resolved"]["gpu_memory_utilization"] == 0.5

    async def test_protected_flag_overlap_reported_not_applied(self):
        """用户撞 protected flag（--model）→ protected_overlap 列出；系统值不被覆盖。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine_params={"extra_args": ["--model", "/hack"]})
        resolved = result["data"]["resolved"]
        assert resolved["protected_overlap"] == ["--model"]
        # 系统 --model 仍在（用户同名被忽略），值未被 /hack 替换
        i = resolved["args"].index("--model")
        assert resolved["args"][i + 1] == "<model-1>"

    async def test_free_flag_passthrough(self):
        """非 protected 自由参数（--dtype）原样透传进最终 args。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine_params={"extra_args": ["--dtype", "half"]})
        assert "--dtype" in result["data"]["resolved"]["args"]

    async def test_user_env_merged(self):
        """engine_params.env 合入最终 env（用户层最高优先）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine_params={"env": {"MY_VAR": "1"}})
        assert result["data"]["resolved"]["env"]["MY_VAR"] == "1"

    async def test_hf_endpoint_overrides_default(self):
        """engine_config.hf_endpoint 作用于真实下发 env，但 resolved.env 不再回显系统键
        （防前端预填回传固化成用户层——回显口径与 engine_params.env 严格一致）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine_config=EngineConfig(hf_endpoint="https://hf-mirror.example"))
        assert result["data"]["resolved"]["env"] == {}

    async def test_model_path_override(self):
        """model_path 覆盖 canonical 路径 → --model / --served-model-name 均跟随。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, model_path="/data/weights/m")
        args = result["data"]["resolved"]["args"]
        assert args[args.index("--model") + 1] == "/data/weights/m"
        assert args[args.index("--served-model-name") + 1] == "/data/weights/m"

    async def test_explicit_image_overrides_catalog(self):
        """image 全量覆盖（escape-hatch）：resolved.image 用覆盖值，卡不走 catalog 灰化。"""
        s = _seed(_M, _node("n1"), _gpu(1, "c1", arch="cann", total=24.0))
        result = await _inspect(s, image="my.custom/vllm:latest")
        d = result["data"]
        assert d["resolved"]["image"] == "my.custom/vllm:latest"
        assert d["cluster_gpus"][0]["grey_reason"] is None  # explicit image 跳过 catalog 灰化


class TestMultinodeEcho:
    """多机拓扑的 dry-run 占位回显与跨子网提醒。"""

    async def test_multinode_placeholder_args_and_warning(self):
        """tp=2×nnodes=2：rendezvous 参数为占位值 + warning 说明部署期定稿（不阻断）。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(
            s,
            gpu_selector=GpuSelector(node_ids=["n1", "n2"]),
            topology=Topology(tp=2, nnodes=2, max_model_len=8192),
        )
        d = result["data"]
        args = d["resolved"]["args"]
        assert args[args.index("--tensor-parallel-size") + 1] == "2"
        assert args[args.index("--master-addr") + 1] == "<master-addr>"
        assert args[args.index("--master-port") + 1] == "29501"
        assert args[args.index("--node-rank") + 1] == "0"
        assert d["warnings"] == [
            "多机 rendezvous 参数（--master-addr/--node-rank/--master-port）为占位值，部署期按实际调度节点定稿"
        ]
        assert d["issues"] == []
        assert d["deployable"] is True

    async def test_cross_subnet_warning(self):
        """多机节点分布跨 /24 子网 → NCCL 可达性提醒（黄灯不阻断）。"""
        s = _seed(
            _M,
            _node("n1", ip="10.0.0.1"),
            _node("n2", ip="192.168.1.2"),
            _gpu(1, "g1"),
            _gpu(2, "g2", "n2"),
        )
        result = await _inspect(
            s,
            gpu_selector=GpuSelector(node_ids=["n1", "n2"]),
            topology=Topology(tp=2, nnodes=2, max_model_len=8192),
        )
        assert any("分布在 2 个 /24 子网" in w and "NCCL 可能不可达" in w for w in result["data"]["warnings"])
        assert result["data"]["deployable"] is True  # 提醒不拦

    async def test_same_subnet_no_warning(self):
        """同子网两节点 → 无跨子网提醒（只剩多机占位提醒）。"""
        s = _seed(
            _M,
            _node("n1", ip="10.0.0.1"),
            _node("n2", ip="10.0.0.2"),
            _gpu(1, "g1"),
            _gpu(2, "g2", "n2"),
        )
        result = await _inspect(
            s,
            gpu_selector=GpuSelector(node_ids=["n1", "n2"]),
            topology=Topology(tp=2, nnodes=2, max_model_len=8192),
        )
        assert not any("子网" in w for w in result["data"]["warnings"])


class TestTopologyStructureValidation:
    """拓扑结构校验（validate_topology 段）：tp 整除 / 引擎能力。"""

    async def test_tp_must_divide_attention_heads(self):
        """heads=8 时 tp=3 切不开 → issue 说明 head 整除约束 + 可用 tp 列表。"""
        m = Model(
            id=1, name="m", model_type="llm", size_gb=30.0, format="safetensors", num_attention_heads=8, num_kv_heads=8
        )
        s = _seed(m, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"), _gpu(3, "g3"))
        result = await _inspect(s, topology=Topology(tp=3, max_model_len=8192))
        d = result["data"]
        assert d["deployable"] is False
        assert any("attention head 数（8）不能被 tp=3 整除" in i and "可用 tp：1、2、4、8" in i for i in d["issues"])

    async def test_invalid_cross_node_topology_raises(self):
        """tp=1×nnodes=2 不满足整除 → 现行为是整个预检 422（L2 记了 issue 但推荐段
        expand_topology 未包 try 重抛）。已知瑕疵：与「结构错误转 issue 呈现」的意图
        相悖，迁移时保持现状、修复另立工单。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        with pytest.raises(LLMPodValidationError, match="必须能被 nnodes"):
            await _inspect(s, topology=Topology(tp=1, nnodes=2, max_model_len=8192))

    async def test_ep_unsupported_raises(self):
        """ep>1 当前不支持 → 422（结构校验先行拒绝）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"))
        with pytest.raises(LLMPodValidationError, match="topology.ep 暂不支持"):
            await _inspect(s, topology=Topology(tp=1, ep=2, max_model_len=8192))


class TestPreviewTopology:
    """preview_topology：推荐集群 + 推荐拓扑的节点图预览（前端画图数据源）。"""

    async def test_preview_carries_cluster_nodes_gpus_and_pseudo_deployment(self):
        """预览含集群 / 节点 / 卡三视图 + 一个 [预览] 假部署（按推荐拓扑调度）。"""
        s = _seed(
            _M,
            Cluster(id=1, name="c1", type="docker", status="active", spec={}),
            _node("n1"),
            _gpu(1, "g1"),
        )
        result = await _inspect(s)
        preview = result["data"]["preview_topology"]
        assert preview is not None
        assert preview["cluster"]["id"] == 1
        assert [n["id"] for n in preview["nodes"]] == ["n1"]
        assert [g["uuid"] for g in preview["gpus"]] == ["g1"]
        dep = preview["deployments"][0]
        assert dep["name"] == "[预览] m"
        assert dep["gpu_uuids"] == ["g1"]
        assert dep["status"] == "creating"

    async def test_preview_failure_degrades_to_none(self):
        """预览构建异常不阻断预检：降级 preview_topology=None（其余字段照常）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))  # 无 Cluster 行 → build 内部 NotFound 被吞
        result = await _inspect(s)
        d = result["data"]
        assert d["preview_topology"] is None
        assert d["deployable"] is True
        assert d["cluster_gpus"]  # 主视图未受影响
