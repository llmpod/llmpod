"""L0 多架构数据层测试：arch/soc_model 数据流 + Ascend SoC 规范化。"""

from __future__ import annotations

import pytest

from llmpod.apps.node.collectors.gpu.ascend import _normalize_soc
from llmpod.tunnel.schemas.system import GpuInfo


class TestNormalizeSoc:
    """Ascend SoC 型号规范化（芯片名 → 镜像 tag 后缀）。"""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("310P3", "310p"),
            ("310P1", "310p"),
            ("310P", "310p"),
            ("910B4", "910b"),
            ("910B1", "910b"),
            ("910A", "910a"),
            ("A3", "a3"),
        ],
    )
    def test_known_models(self, name: str, expected: str):
        assert _normalize_soc(name) == expected

    def test_case_insensitive(self):
        assert _normalize_soc("310p3") == "310p"
        assert _normalize_soc("910b4") == "910b"

    def test_unknown_fallback(self):
        # 未知型号兜底取首个 token 小写
        assert _normalize_soc("FooBar") == "foobar"

    def test_empty(self):
        assert _normalize_soc("") == ""


class TestGpuInfoArch:
    """GpuInfo 的 arch/soc_model 字段。"""

    def test_defaults(self):
        gpu = GpuInfo(uuid="x", index=0, name="X")
        assert gpu.arch == ""  # 默认空（collector 必填，sync_gpus 缺失则跳过该卡+告警，不中断注册）
        assert gpu.soc_model is None

    def test_nvidia_arch(self):
        gpu = GpuInfo(uuid="x", index=0, name="A100", arch="nvidia")
        assert gpu.arch == "nvidia"
        assert gpu.soc_model is None  # NVIDIA 不填 soc_model

    def test_ascend_arch_and_soc(self):
        gpu = GpuInfo(uuid="x", index=0, name="310P3", arch="cann", soc_model="310p")
        assert gpu.arch == "cann"
        assert gpu.soc_model == "310p"
