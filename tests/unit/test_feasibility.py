"""可部署性检测纯逻辑单测（无 DB / 无网络）。

覆盖 ``_assess_groups``（同集群+同架构+同显存分组求和、0.9 阈值 can_fit/deficit）、
``assess_engine_support``（type+format 判定）、``_deployable_clusters``（架构×引擎组合）、
``_assess_offload_nodes``（GGUF per-node VRAM+RAM）、``assess_feasibility`` 装配层
（cluster_id 过滤 + context_size KV + 0.9 阈值）。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.feasibility import (
    EngineAssessment,
    GPUResource,
    NodeResource,
    _assess_groups,
    _assess_offload_nodes,
    _deployable_clusters,
    assess_engine_support,
)
from llmpod.apps.manager.deployment.engine.base import estimate_required_vram


# ============ estimate_required_vram（KV 粗估，与 feasibility 同源） ============


class TestEstimateRequiredVram:
    def test_weight_only_when_no_context(self):
        # inspect 时 context 未知 → KV=0，乐观裸权重
        assert estimate_required_vram(10.0, None) == 10.0

    def test_weight_plus_kv_at_base_context(self):
        # 10GB 权重 @ 8192 context → KV = 10×0.1×1 = 1，总 11
        assert estimate_required_vram(10.0, 8192) == 11.0

    def test_kv_scales_with_context(self):
        # @32768 (4× base) → KV = 10×0.1×4 = 4，总 14
        assert estimate_required_vram(10.0, 32768) == 14.0

    def test_size_unknown_returns_none(self):
        assert estimate_required_vram(None, 8192) is None

    def test_pooling_skips_kv(self):
        # embedding/rerank 类别非自回归，无 KV cache：context 再长也只算权重
        assert estimate_required_vram(10.0, 32768, has_kv_cache=False) == 10.0


# ============ _assess_groups ============


def _g(cluster, arch, vram, used, name="GPU"):
    return GPUResource(
        cluster_id=cluster,
        cluster_name=f"c{cluster}",
        arch=arch,
        total_vram_gb=vram,
        name=name,
        used_vram_gb=used,
    )


class TestAssessGroups:
    def test_can_fit_uses_0_9_threshold(self):
        # avail 100，cap=90。required 89 ≤ 90 → fit；required 91 > 90 → 不 fit
        groups = _assess_groups([_g(1, "nvidia", 100.0, 0.0)], 89.0)
        assert groups[0].can_fit is True
        groups = _assess_groups([_g(1, "nvidia", 100.0, 0.0)], 91.0)
        assert groups[0].can_fit is False
        assert groups[0].deficit_gb == 1.0  # 91 - 90

    def test_same_group_sums_available(self):
        # 两张 80G，used 10+5 → avail 145，cap 130.5
        groups = _assess_groups([_g(1, "nvidia", 80.0, 10.0), _g(1, "nvidia", 80.0, 5.0)], 130.0)
        assert len(groups) == 1
        g = groups[0]
        assert g.online_gpus == 2
        assert g.available_vram_gb == 145.0
        assert g.can_fit is True  # 130 ≤ 130.5
        assert g.deficit_gb == 0.0

    def test_split_by_arch(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 0.0), _g(1, "cann", 32.0, 0.0)], 10.0)
        assert {g.arch for g in groups} == {"nvidia", "cann"}

    def test_split_by_vram_size(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 0.0), _g(1, "nvidia", 40.0, 0.0)], 10.0)
        assert len(groups) == 2
        assert {g.vram_gb for g in groups} == {80.0, 40.0}

    def test_split_by_cluster(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 0.0), _g(2, "nvidia", 80.0, 0.0)], 10.0)
        assert {g.cluster_id for g in groups} == {1, 2}

    def test_no_realtime_when_used_none(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, None)], 50.0)
        assert groups[0].realtime is False
        assert groups[0].available_vram_gb == 80.0  # 乐观按 total
        assert groups[0].can_fit is True  # 50 ≤ 72 (80×0.9)

    def test_required_none_skips_vram_check(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 10.0)], None)
        assert groups[0].can_fit is None
        assert groups[0].deficit_gb is None

    def test_mixed_gpu_names_visible(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 0.0, "A100-80G"), _g(1, "nvidia", 80.0, 0.0, "H100-80G")], 10.0)
        assert groups[0].gpu_names == ["A100-80G", "H100-80G"]


# ============ assess_engine_support ============


class TestAssessEngines:
    def test_llm_safetensors(self):
        e = {x.engine: x for x in assess_engine_support("llm", "safetensors")}
        assert e["vllm"].supported is True
        assert e["sglang"].supported is True
        assert e["llamacpp"].supported is False  # 格式 gguf only

    def test_llm_gguf(self):
        e = {x.engine: x for x in assess_engine_support("llm", "gguf")}
        assert e["llamacpp"].supported is True
        assert e["vllm"].supported is False
        assert e["sglang"].supported is False

    def test_embedding_safetensors(self):
        e = {x.engine: x for x in assess_engine_support("embedding", "safetensors")}
        assert e["vllm"].supported is True
        assert e["sglang"].supported is True
        assert e["llamacpp"].supported is False  # 格式 gguf only

    def test_unknown_type_unsupported(self):
        e = {x.engine: x for x in assess_engine_support("image", "safetensors")}
        assert e["vllm"].supported is False
        assert any("类别" in r for r in e["vllm"].reasons)

    def test_indeterminate_when_none(self):
        for x in assess_engine_support(None, None):
            assert x.supported is None
            assert x.reasons

    def test_partial_indeterminate(self):
        for x in assess_engine_support("llm", None):
            assert x.supported is None


# ============ _deployable_clusters ============


class TestDeployableClusters:
    def _engines(self, supported_map: dict[str, bool | None]) -> list[EngineAssessment]:
        return [EngineAssessment(engine=k, supported=v, reasons=[]) for k, v in supported_map.items()]

    def test_nvidia_group_with_supported_engine(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 0.0)], 50.0)  # cap 72 ≥ 50 → fit
        engines = self._engines({"vllm": True, "sglang": True, "llamacpp": False})
        assert _deployable_clusters(groups, engines, 50.0) == [1]

    def test_rocm_group_no_engine_supports(self):
        # vllm 现支持 cann；rocm 无引擎支持 → 不可部署
        groups = _assess_groups([_g(1, "rocm", 32.0, 0.0)], 10.0)
        engines = self._engines({"vllm": True, "sglang": True, "llamacpp": True})
        assert _deployable_clusters(groups, engines, 10.0) == []

    def test_cpu_group_supported_by_llamacpp(self):
        groups = _assess_groups([_g(1, "cpu", 64.0, 0.0)], 10.0)
        engines = self._engines({"vllm": True, "sglang": True, "llamacpp": True})
        assert _deployable_clusters(groups, engines, 10.0) == [1]

    def test_required_none_returns_empty(self):
        groups = _assess_groups([_g(1, "nvidia", 80.0, 0.0)], None)
        engines = self._engines({"vllm": True})
        assert _deployable_clusters(groups, engines, None) == []

    def test_group_not_fit_excluded(self):
        groups = _assess_groups([_g(1, "nvidia", 10.0, 0.0)], 100.0)  # cap 9 < 100
        engines = self._engines({"vllm": True})
        assert _deployable_clusters(groups, engines, 100.0) == []


# ============ _assess_offload_nodes（GGUF per-node offload） ============


def _nf(cluster, node, vram, ram, nv=1, host="n", max_card=None, mem_total=None):
    """节点资源桩：``vram`` 是节点可用显存之和；``max_card`` 缺省 = 单卡（与之和相等）；
    ``mem_total`` 是节点总内存（账本口径基数，缺省 = ``ram``）。"""
    return NodeResource(
        cluster_id=cluster,
        cluster_name=f"c{cluster}",
        node_id=node,
        hostname=host,
        nvidia_gpus=nv,
        avail_vram_gb=vram,
        max_card_vram_gb=vram if max_card is None else max_card,
        avail_ram_gb=ram,
        memory_gb=ram if mem_total is None else mem_total,
        realtime_vram=True,
        realtime_ram=True,
    )


class TestAssessOffloadNodes:
    """形状未知（无 total_layers）的粗估口径：两端取解（全量上卡=单卡显存、纯 CPU=内存）。"""

    def test_full_gpu_uses_0_9_threshold(self):
        # vram 100，cap 90。required 89 ≤ 90 → fit_full_gpu；91 → 不
        nodes = _assess_offload_nodes([_nf(1, "n1", 100.0, 0.0)], 89.0)
        assert nodes[0].fit_full_gpu is True
        nodes = _assess_offload_nodes([_nf(1, "n1", 100.0, 0.0)], 91.0)
        assert nodes[0].fit_full_gpu is False

    def test_shape_unknown_needs_one_real_lane(self):
        """回归：合并池（显存+内存）判可行的是真实系统里不存在的混合形态——两端都
        单独装不下时不得判可部署（旧口径 combined×0.9 放行、创建必败）。"""
        # vram 40 (cap 36) + ram 64 (cap 57.6)：required 90 两端都装不下
        nodes = _assess_offload_nodes([_nf(1, "n1", 40.0, 64.0)], 90.0, allow_cpu_lane=True)
        assert nodes[0].memory is None
        assert nodes[0].fit_full_gpu is False
        assert nodes[0].fit_offload is False

    def test_shape_unknown_cpu_lane_fits_when_authorized(self):
        """未授权 CPU（默认）时只有显存一端参与判定；显存装不下 → 不可部署。"""
        nodes = _assess_offload_nodes([_nf(1, "n1", 40.0, 64.0)], 50.0)
        assert nodes[0].fit_offload is False  # 内存 57.6 装得下但没授权，不算能部署
        nodes = _assess_offload_nodes([_nf(1, "n1", 40.0, 64.0)], 50.0, allow_cpu_lane=True)
        assert nodes[0].fit_offload is True  # 授权后纯 CPU 端可行

    def test_cpu_only_node_can_offload(self):
        nodes = _assess_offload_nodes([_nf(1, "n1", 0.0, 256.0, nv=0)], 200.0, allow_cpu_lane=True)  # cap 230.4 ≥ 200
        assert nodes[0].nvidia_gpus == 0
        assert nodes[0].fit_full_gpu is False
        assert nodes[0].fit_offload is True

    def test_neither_fits_reports_deficit(self):
        # 两端 cap（显存 7.2 / 内存 14.4，未授权 CPU 只看显存端）都 < 100 → 缺口按显存端
        nodes = _assess_offload_nodes([_nf(1, "n1", 8.0, 16.0)], 100.0)
        assert nodes[0].fit_full_gpu is False
        assert nodes[0].fit_offload is False
        assert nodes[0].deficit_gb == 92.8  # 100 − 8×0.9

    def test_sorted_by_combined_desc(self):
        nodes = _assess_offload_nodes(
            [_nf(1, "small", 8.0, 16.0), _nf(1, "big", 40.0, 64.0), _nf(1, "mid", 20.0, 30.0)], 10.0
        )
        assert [n.node_id for n in nodes] == ["big", "mid", "small"]

    def test_required_none_skips_fit(self):
        nodes = _assess_offload_nodes([_nf(1, "n1", 40.0, 64.0)], None)
        assert nodes[0].fit_full_gpu is None
        assert nodes[0].fit_offload is None
        assert nodes[0].deficit_gb is None


class TestAssessOffloadNodesTiers:
    """架构形状已知 → 逐层 offload 模型（权重可拆、KV 不可拆）。"""

    KW = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10)

    def test_tier_table_attached(self):
        nodes = _assess_offload_nodes([_nf(1, "n1", 200.0, 100.0)], None, **self.KW)
        memory = nodes[0].memory
        assert memory is not None
        assert memory.scenario == "gpu_only"
        assert [t.offload_layers for t in memory.tiers] == list(range(11))
        assert nodes[0].fit_full_gpu is True and nodes[0].fit_offload is True

    def test_hybrid_node_fits_offload_but_not_full_gpu(self):
        # 显存 cap 90 装不下满 offload（110），内存充裕 → auto 只取两端，落纯 CPU 档
        nodes = _assess_offload_nodes([_nf(1, "n1", 100.0, 200.0)], None, allow_cpu_lane=True, **self.KW)
        assert nodes[0].fit_full_gpu is False
        assert nodes[0].fit_offload is True
        assert nodes[0].memory is not None and nodes[0].memory.scenario == "cpu_only"

    def test_cpu_only_node_fits(self):
        nodes = _assess_offload_nodes([_nf(1, "n1", 0.0, 256.0, nv=0)], None, allow_cpu_lane=True, **self.KW)
        assert nodes[0].fit_full_gpu is False
        assert nodes[0].fit_offload is True
        assert nodes[0].memory is not None and nodes[0].memory.scenario == "cpu_only"

    def test_cpu_end_requires_lane_authorization(self):
        """请求未放行 CPU 部署（allow_cpu=False）：只有纯 CPU 档可行的节点不算「能部署」——
        否则预检放行的请求会在调度时被拒（诊断与求解同口径）。"""
        node = _nf(1, "n1", 100.0, 200.0)
        assert _assess_offload_nodes([node], None, **self.KW)[0].fit_offload is False
        # 逐层表本身照常产出（表是「能不能放」的事实，部署授权是调用方的事）
        assert _assess_offload_nodes([node], None, **self.KW)[0].memory is not None

    def test_multi_card_uses_per_card_capacity(self):
        """节点显存按**单卡**判定：2×24GB 节点跑 tp=1 的 34GB 模型装不下（按节点求和
        44×0.9=39.6 会误判可行，实际一块卡放不下）。"""
        node = _nf(1, "n1", 44.0, 40.0, nv=2, max_card=22.0)
        kw = dict(weight_gb=32.0, kv_gb=2.0, total_layers=32)
        assert _assess_offload_nodes([node], None, allow_cpu_lane=True, **kw)[0].fit_full_gpu is False
        # tp=2：每卡 17GB ≤ 22×0.9 → 可行（逐层表按 tp×pp 均摊，与 estimate_vram_gb 同口径）
        assert _assess_offload_nodes([node], None, allow_cpu_lane=True, parallel=2, **kw)[0].fit_full_gpu is True

    def test_shape_unknown_falls_back_to_tierless_estimate(self):
        # total_layers=None → 不产 memory，粗估两端取解（见 TestAssessOffloadNodes 形状未知组）
        nodes = _assess_offload_nodes([_nf(1, "n1", 40.0, 64.0)], 50.0, allow_cpu_lane=True)
        assert nodes[0].memory is None
        assert nodes[0].fit_full_gpu is False and nodes[0].fit_offload is True


# ============ assess_feasibility（装配层：cluster_id + context_size + 0.9） ============


class TestAssessFeasibility:
    async def test_deployable_with_context_kv(self, monkeypatch):
        import llmpod.apps.manager.deployment.feasibility as feas

        async def _fake_collect(db, cluster_id=None, *, used_vram=None):
            return [_g(1, "nvidia", 80.0, 10.0), _g(1, "nvidia", 80.0, 5.0)]  # avail 145, cap 130.5

        monkeypatch.setattr(feas, "_collect_gpu_resources", _fake_collect)
        # 10GB 权重 + ctx 8192 → required 11（含 KV）；cap 130.5 ≥ 11 → deployable
        report = await feas.assess_feasibility(
            db=None, model_size_gb=10.0, model_type="llm", model_format="safetensors", context_size=8192
        )
        assert report.deployable is True
        assert report.required_vram_gb == 11.0
        assert report.context_size == 8192
        assert 1 in report.deployable_clusters

    async def test_kv_dtype_bytes_and_arch_meta(self, monkeypatch):
        """KV 精度档走 dtype_bytes（启发式折算）；架构形状齐备时走真公式。"""
        import llmpod.apps.manager.deployment.feasibility as feas
        from llmpod.apps.manager.deployment import resource

        async def _fake_collect(db, cluster_id=None, *, used_vram=None):
            return [_g(1, "nvidia", 80.0, 0.0)]

        monkeypatch.setattr(feas, "_collect_gpu_resources", _fake_collect)
        # 10GB 权重 @8192 → KV 基数 1GB；fp8（1 字节）→ 10.5
        report = await feas.assess_feasibility(
            db=None,
            model_size_gb=10.0,
            model_type="llm",
            model_format="safetensors",
            context_size=8192,
            kv_dtype_bytes=1.0,
        )
        assert report.required_vram_gb == 10.5

        # 真公式：2 × n_seq × 8192 × 32 层 × 8 KV head × 128 dim × 1B；
        # 默认按一条满长 = 0.5GB → 10.5，显式 4 条满长 = 2GB → 12.0
        meta = resource.KVArchMeta(num_layers=32, num_kv_heads=8, head_dim=128)
        for n_parallel, expected in ((None, 10.5), (4, 12.0)):
            report = await feas.assess_feasibility(
                db=None,
                model_size_gb=10.0,
                model_type="llm",
                model_format="safetensors",
                context_size=8192,
                kv_dtype_bytes=1.0,
                arch_meta=meta,
                n_parallel=n_parallel,
            )
            assert report.required_vram_gb == expected

    async def test_not_deployable_when_required_exceeds_cap(self, monkeypatch):
        import llmpod.apps.manager.deployment.feasibility as feas

        async def _fake_collect(db, cluster_id=None, *, used_vram=None):
            return [_g(1, "nvidia", 100.0, 0.0)]  # avail 100, cap 90

        monkeypatch.setattr(feas, "_collect_gpu_resources", _fake_collect)
        # required 95 > cap 90 → 不 deployable
        report = await feas.assess_feasibility(
            db=None, model_size_gb=95.0, model_type="llm", model_format="safetensors"
        )
        assert report.deployable is False
        assert report.deployable_clusters == []

    async def test_not_deployable_when_no_engine_supports_type(self, monkeypatch):
        import llmpod.apps.manager.deployment.feasibility as feas

        async def _fake_collect(db, cluster_id=None, *, used_vram=None):
            return [_g(1, "nvidia", 80.0, 0.0)]

        monkeypatch.setattr(feas, "_collect_gpu_resources", _fake_collect)
        report = await feas.assess_feasibility(
            db=None, model_size_gb=10.0, model_type="image", model_format="safetensors"
        )
        assert report.deployable is False
        assert report.deployable_clusters == []

    async def test_size_unknown_skips_vram(self, monkeypatch):
        import llmpod.apps.manager.deployment.feasibility as feas

        async def _fake_collect(db, cluster_id=None, *, used_vram=None):
            return [_g(1, "nvidia", 80.0, 0.0)]

        monkeypatch.setattr(feas, "_collect_gpu_resources", _fake_collect)
        report = await feas.assess_feasibility(db=None, model_size_gb=None, model_type=None, model_format=None)
        assert report.required_vram_gb is None
        assert report.deployable is False
        assert all(e.supported is None for e in report.engines)

    async def test_cluster_id_filters_collect(self, monkeypatch):
        """传 cluster_id 时 _collect_* 收到该参数（deploy 指定集群预检）。"""
        import llmpod.apps.manager.deployment.feasibility as feas

        seen: list[int | None] = []

        async def _fake_collect(db, cluster_id=None, *, used_vram=None):
            seen.append(cluster_id)
            return []  # 该集群无 GPU → not deployable

        monkeypatch.setattr(feas, "_collect_gpu_resources", _fake_collect)
        await feas.assess_feasibility(
            db=None, model_size_gb=10.0, model_type="llm", model_format="safetensors", cluster_id=7
        )
        assert seen == [7]  # cluster_id 透传到 _collect

    async def test_gguf_uses_offload_with_context(self, monkeypatch):
        import llmpod.apps.manager.deployment.feasibility as feas

        async def _fake_nodes(db, cluster_id=None, **_kw):
            return [_nf(1, "n1", 40.0, 100.0)]  # 显存 cap 36 / 内存 cap 90

        monkeypatch.setattr(feas, "collect_node_resources", _fake_nodes)
        # 80GB GGUF + ctx 8192 → required 88：显存端装不下、授权 CPU 后内存端 90 ≥ 88 → fit_offload
        report = await feas.assess_feasibility(
            db=None,
            model_size_gb=80.0,
            model_type="llm",
            model_format="gguf",
            context_size=8192,
            allow_cpu=True,
        )
        assert report.required_vram_gb == 88.0
        assert report.offload_nodes[0].fit_offload is True
        assert report.deployable is True
        assert report.gpu_groups == []  # GGUF 不走 GPU 分组

    async def test_cpu_only_gguf_small_model_deployable(self, monkeypatch):
        """小 GGUF + 显式 cpu_only（无 allow_cpu 字段）→ 预检必须判可部署。

        ``wants_cpu_lane`` 的语义是 allow_cpu 或显式 cpu_only 任一即放行；预检
        入口传 ``allow_cpu=cpu_lane_requested``（谓词同一权威出处），漏传会让
        cpu_only 请求在内存充足的在线节点上仍报「无在线可用节点」422。
        """
        import llmpod.apps.manager.deployment.feasibility as feas
        from llmpod.apps.manager.deployment import resource as R

        async def _fake_nodes(db, cluster_id=None, **_kw):
            # 两节点在线、内存充足、有 nvidia 卡（e2e 环境形态）
            return [_nf(1, "n1", 11.0, 50.0), _nf(1, "n2", 11.0, 50.0)]

        monkeypatch.setattr(feas, "collect_node_resources", _fake_nodes)
        report = await feas.assess_feasibility(
            db=None,
            model_size_gb=0.5,
            model_type="llm",
            model_format="gguf",
            context_size=4096,
            total_layers=26,
            # 显式 cpu_only（不带 allow_cpu）：与 create 路由同参
            allow_cpu=R.wants_cpu_lane(False, R.SCENARIO_CPU_ONLY),
            parallel=1,
        )
        assert report.deployable is True, report.message
        assert report.offload_nodes, "在线节点须进入逐层评估"

    async def test_cpu_only_no_online_nodes_undeployable(self, monkeypatch):
        """真无在线节点：cpu_only 也不可部署（无在线可用节点语义保留）。"""
        import llmpod.apps.manager.deployment.feasibility as feas
        from llmpod.apps.manager.deployment import resource as R

        async def _fake_nodes(db, cluster_id=None, **_kw):
            return []

        monkeypatch.setattr(feas, "collect_node_resources", _fake_nodes)
        report = await feas.assess_feasibility(
            db=None,
            model_size_gb=0.5,
            model_type="llm",
            model_format="gguf",
            context_size=4096,
            total_layers=26,
            allow_cpu=R.wants_cpu_lane(False, R.SCENARIO_CPU_ONLY),
        )
        assert report.deployable is False
        assert "无在线可用节点" in (report.message or "")
