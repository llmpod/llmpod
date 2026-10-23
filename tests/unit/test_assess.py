"""assess.py 纯函数单测：逐卡匹配评估（灰化语义 / fit / group_key / 所需显存）。

零 DB——用 dataclass mock 卡直接喂入。
"""

from __future__ import annotations

from dataclasses import dataclass

from llmpod.apps.manager.deployment.assess import assess_per_gpu


@dataclass
class _Card:
    uuid: str
    node_id: str = "n1"
    cluster_id: int = 1
    index: int = 0
    arch: str = "nvidia"
    total_vram_gb: float = 24.0
    available_vram_gb: float = 24.0
    status: str = "online"
    name: str = ""  # GPU 型号（catalog gpu_model glob 匹配用）
    driver_version: str | None = None  # 驱动/CANN 版本（catalog 驱动区间匹配用）


class TestAssessPerGpu:
    def test_arch_mismatch(self):
        """vllm 支持 nvidia/cann，rocm 卡 → grey=arch_mismatch、不可选。"""
        a = assess_per_gpu([_Card(uuid="g0", arch="rocm")], engine="vllm", model_size_gb=10.0, context_size=None)
        assert a[0].grey_reason == "arch_mismatch"
        assert a[0].fit is False
        assert a[0].group_key == "1:rocm:24.0"

    def test_offline(self):
        """离线卡 → grey=offline、group_key=None。"""
        a = assess_per_gpu([_Card(uuid="g0", status="offline")], engine="vllm", model_size_gb=10.0, context_size=None)
        assert a[0].grey_reason == "offline"
        assert a[0].fit is False
        assert a[0].group_key is None

    def test_fit_single_card_sufficient(self):
        """单卡空闲（可用罩得住整卡预留）且预留装得下所需 → fit=True、grey=None。"""
        a = assess_per_gpu(
            [_Card(uuid="g0", available_vram_gb=24.0)], engine="vllm", model_size_gb=10.0, context_size=None
        )
        assert a[0].grey_reason is None
        assert a[0].fit is True
        assert a[0].required_vram_gb == 10.0  # ctx=None → KV=0

    def test_fit_false_when_small_but_group_viable(self):
        """卡显存不足以单卡承载，但仍可选用（TP 分摊）→ fit=False、grey=None（不灰）。"""
        # 所需 40G，单卡整卡预留 24×0.9=21.6 < 40 → fit=False，但 grey=None（组内可凑）
        a = assess_per_gpu(
            [_Card(uuid="g0", available_vram_gb=24.0)], engine="vllm", model_size_gb=40.0, context_size=None
        )
        assert a[0].grey_reason is None
        assert a[0].fit is False

    def test_fit_false_when_occupied_card(self):
        """被占超过一成的卡 fit=False（可用量罩不住整卡预留），不按总量乐观。

        旧口径 available×0.9 ≥ 所需（12×0.9=10.8 ≥ 10）会把这张卡标成「单卡可用」，
        而调度按整卡预留必拦下——预览说能放、创建放不下。
        """
        a = assess_per_gpu(
            [_Card(uuid="g0", available_vram_gb=12.0)], engine="vllm", model_size_gb=10.0, context_size=None
        )
        assert a[0].grey_reason is None  # 不灰（组内可凑 / 用户手选共址）
        assert a[0].fit is False

    def test_group_key_same_group(self):
        """同 cluster+同 arch+同 total_vram → 同 group_key；total 不同 → 不同组。"""
        cards = [
            _Card(uuid="g0", total_vram_gb=24.0),
            _Card(uuid="g1", total_vram_gb=24.0),
            _Card(uuid="g2", total_vram_gb=40.0),
        ]
        a = assess_per_gpu(cards, engine="vllm", model_size_gb=10.0, context_size=None)
        keys = [x.group_key for x in a]
        assert keys[0] == keys[1] == "1:nvidia:24.0"
        assert keys[2] == "1:nvidia:40.0"

    def test_size_unknown_optimistic(self):
        """size 未知 → required=None、fit 乐观 True、仍可判定架构/离线。"""
        a = assess_per_gpu([_Card(uuid="g0")], engine="vllm", model_size_gb=None, context_size=None)
        assert a[0].required_vram_gb is None
        assert a[0].fit is True
        assert a[0].grey_reason is None

    def test_context_increases_required(self):
        """context 越大 KV 越大 → required_vram_gb 增加。"""
        no_ctx = assess_per_gpu([_Card(uuid="g0")], engine="vllm", model_size_gb=10.0, context_size=None)[0]
        with_ctx = assess_per_gpu([_Card(uuid="g0")], engine="vllm", model_size_gb=10.0, context_size=8192)[0]
        big_ctx = assess_per_gpu([_Card(uuid="g0")], engine="vllm", model_size_gb=10.0, context_size=32768)[0]
        assert no_ctx.required_vram_gb == 10.0  # KV=0
        assert with_ctx.required_vram_gb == 11.0  # 10 + 10×0.1×(8192/8192) = 11
        assert big_ctx.required_vram_gb > with_ctx.required_vram_gb  # KV 随 ctx 线性涨

    def test_kv_dtype_bytes_scales_required(self):
        """KV 精度档折算：fp8（1 字节）→ KV 项减半。"""
        a = assess_per_gpu([_Card(uuid="g0")], engine="vllm", model_size_gb=10.0, context_size=8192, kv_dtype_bytes=1.0)
        assert a[0].required_vram_gb == 10.5

    def test_engine_none_skips_arch_check(self):
        """engine=None → 不做架构判定，cann 卡也不报 arch_mismatch。"""
        a = assess_per_gpu([_Card(uuid="g0", arch="cann")], engine=None, model_size_gb=10.0, context_size=None)
        assert a[0].grey_reason is None

    def test_cluster_id_fallback(self):
        """卡对象无 cluster_id 时用传入的 cluster_id 拼 group_key。"""

        @dataclass
        class _Bare:
            uuid: str = "g0"
            arch: str = "nvidia"
            total_vram_gb: float = 24.0
            available_vram_gb: float = 24.0
            status: str = "online"

        a = assess_per_gpu([_Bare()], engine="vllm", model_size_gb=10.0, context_size=None, cluster_id=7)
        assert a[0].group_key == "7:nvidia:24.0"

    # ---- 逐卡镜像支持（catalog 按型号/驱动/engine_version 判）----

    def test_no_image_unsupported_gpu_model(self):
        """cann 卡型号不在任何 catalog glob（如 910D）→ grey=no_image + grey_detail 人读原因。"""
        a = assess_per_gpu(
            [_Card(uuid="g0", arch="cann", name="Ascend 910D")], engine="vllm", model_size_gb=10.0, context_size=None
        )
        assert a[0].grey_reason == "no_image"
        assert a[0].grey_detail
        assert a[0].fit is False
        assert a[0].group_key == "1:cann:24.0"

    def test_no_image_wrong_engine_version(self):
        """型号匹配但 engine_version 无对应条目（310P + vllm/cann@0.20.0）→ no_image。

        310P3 在 0.22.1/0.23.0/0.26.0/0.27.1 均有条目（高版本为复用镜像的补位条目），
        用一个确无条目的版本验证 no_image 分支。
        """
        a = assess_per_gpu(
            [_Card(uuid="g0", arch="cann", name="Ascend 310P3", driver_version="25.2.3")],
            engine="vllm",
            model_size_gb=10.0,
            context_size=None,
            engine_version="0.20.0",
        )
        assert a[0].grey_reason == "no_image"
        assert "0.20.0" in a[0].grey_detail

    def test_cann_matching_card_selectable(self):
        """310P3 + 合法 engine_version（0.22.1）→ 可选（grey=None）。"""
        a = assess_per_gpu(
            [_Card(uuid="g0", arch="cann", name="Ascend 310P3", driver_version="25.2.3")],
            engine="vllm",
            model_size_gb=10.0,
            context_size=None,
            engine_version="0.22.1",
        )
        assert a[0].grey_reason is None
        assert a[0].grey_detail is None

    def test_explicit_image_bypasses_catalog(self):
        """explicit_image 覆盖 catalog（escape-hatch）→ 即便型号无匹配也不灰 no_image。"""
        a = assess_per_gpu(
            [_Card(uuid="g0", arch="cann", name="Ascend 910D")],
            engine="vllm",
            model_size_gb=10.0,
            context_size=None,
            explicit_image="custom:img",
        )
        assert a[0].grey_reason is None

    def test_nvidia_card_always_resolvable(self):
        """nvidia 卡无 driver → catalog default 条目兜底，仍可选（不误灰）。"""
        a = assess_per_gpu(
            [_Card(uuid="g0", name="A100", driver_version=None)], engine="vllm", model_size_gb=10.0, context_size=None
        )
        assert a[0].grey_reason is None
