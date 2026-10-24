"""镜像 catalog resolver + 列举端点 + 三层覆盖 单测。"""

from __future__ import annotations

from llmpod.apps.manager.deployment import image_catalog
from llmpod.apps.manager.deployment.engine.base import merge_start_args
from llmpod.apps.manager.deployment.image_catalog import ImageEntry, resolve_images, list_entries


class TestResolve:
    def test_nvidia_picks_by_driver(self):
        # 2026-09-24 RTX 2080Ti 换驱实测阶梯：
        # ≥580 cu130 默认；575-579 cu129；570-574 cu128；550-569 cu124；525-549 cu121；<525 无解
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="595.84")
            == "vllm/vllm-openai:v0.27.1"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="575.51")
            == "vllm/vllm-openai:v0.27.1-cu129"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="570.21") == "vllm/vllm-openai:v0.9.0"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="550.16") == "vllm/vllm-openai:v0.8.5"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="535.29") == "vllm/vllm-openai:v0.7.3"
        )
        # R4xx/5xx-早期（cu121 MVC 地板 525 以下）：官方 vLLM 无任何可用镜像 → 解析无命中
        assert image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="470.256.02") is None
        assert image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="520.61.05") is None

    def test_nvidia_no_driver_falls_to_default(self):
        # 节点未上报驱动 → 保守放行（不按 driver 过滤）；default 优先（driver 未知时
        # driver-constrained 条目不越过 default，避免误选旧驱动镜像）
        assert image_catalog.pick_image(engine="vllm", arch="nvidia") == "vllm/vllm-openai:v0.27.1"

    def test_sglang_picks_by_driver(self):
        assert (
            image_catalog.pick_image(
                engine="sglang", arch="nvidia", gpu_model="NVIDIA A100-80G", driver_version="595.84"
            )
            == "lmsysorg/sglang:v0.5.17"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang", arch="nvidia", gpu_model="NVIDIA A100-80G", driver_version="575.51"
            )
            == "lmsysorg/sglang:v0.5.17-cu129"
        )
        assert (
            image_catalog.pick_image(engine="sglang", arch="nvidia", driver_version="565.77")
            == "lmsysorg/sglang:v0.4.10-cu126"
        )
        assert (
            image_catalog.pick_image(engine="sglang", arch="nvidia", driver_version="535.29")
            == "lmsysorg/sglang:v0.4.6.post1-cu121"
        )
        # cu118 线原生地板 520（470 实测 CUDA init 失败）
        assert (
            image_catalog.pick_image(engine="sglang", arch="nvidia", driver_version="520.61")
            == "lmsysorg/sglang:v0.4.2-cu118"
        )
        assert image_catalog.pick_image(engine="sglang", arch="nvidia", driver_version="470.256.02") is None

    def test_sglang_turing_pins_04x(self):
        """SGLang 0.5.x 在 sm75(Turing) 崩（sgl_kernel cutlass DSL 无 sm_75，595.84 实测起服务即崩）；
        Turing 卡命中具体 gpu_model 条目回落 0.4.10-cu126，非 Turing 卡不受影响。"""
        assert (
            image_catalog.pick_image(
                engine="sglang", arch="nvidia", driver_version="595.84", gpu_model="NVIDIA GeForce RTX 2080 Ti"
            )
            == "lmsysorg/sglang:v0.4.10-cu126"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang", arch="nvidia", driver_version="575.51", gpu_model="NVIDIA GeForce RTX 2080 Ti"
            )
            == "lmsysorg/sglang:v0.4.10-cu126"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang", arch="nvidia", driver_version="595.84", gpu_model="NVIDIA A100-80G"
            )
            == "lmsysorg/sglang:v0.5.17"
        )

    def test_llamacpp_copper_ladder(self):
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="575.64")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="579.99")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="580.00")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda13"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="595.84")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda13"
        )
        assert image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="569.99") is None

    def test_cann_by_soc_model(self):
        assert (
            image_catalog.pick_image(engine="vllm", arch="cann", gpu_model="310P3", driver_version="25.2.3")
            == "vllm-ascend-310p:fixed"
        )
        assert image_catalog.pick_image(
            engine="vllm", arch="cann", gpu_model="Ascend 910B", driver_version="24.1.0"
        ).endswith("v0.23.0rc1")
        assert image_catalog.pick_image(
            engine="vllm", arch="cann", gpu_model="Atlas A3 910C", driver_version="24.1.0"
        ).endswith("-a3")
        assert image_catalog.pick_image(
            engine="vllm", arch="cann", gpu_model="Atlas A5 950DT", driver_version="26.0.0"
        ).endswith("-950dt")

    def test_specificity_gpu_specific_ranks_higher(self):
        # cann 910b/910c gpu_model 具体；default 910b 排在前面
        items = resolve_images(engine="vllm", arch="cann", gpu_model="Ascend 910B")
        assert items[0].id == "vllm-ascend-910b"

    def test_engine_version_filter(self):
        # 0.23.0 has official CUDA 13.0 and CUDA 12.9 variants; the nonexistent cu121 tag stays removed.
        # With no driver, the new CUDA 12.9 variant is withheld until its floor can be checked.
        items = resolve_images(engine="vllm", arch="nvidia", engine_version="0.23.0")
        assert {e.id for e in items} == {"vllm-nvidia-default"}
        # 0.27.1 双通道：cu130（≥580）+ cu129（≥575 兜底）
        items = resolve_images(engine="vllm", arch="nvidia", engine_version="0.27.1")
        assert {e.id for e in items} == {"vllm-0271-nvidia-cu130", "vllm-nvidia-cu129"}

    def test_added_vllm_versions_use_official_exact_tags(self):
        expected = {
            "0.9.1": "vllm/vllm-openai:v0.9.1",
            "0.9.2": "vllm/vllm-openai:v0.9.2",
            "0.10.0": "vllm/vllm-openai:v0.10.0",
            "0.10.1": "vllm/vllm-openai:v0.10.1",
            "0.10.1.1": "vllm/vllm-openai:v0.10.1.1",
            "0.10.2": "vllm/vllm-openai:v0.10.2",
            "0.11.0": "vllm/vllm-openai:v0.11.0",
            "0.11.1": "vllm/vllm-openai:v0.11.1",
            "0.11.2": "vllm/vllm-openai:v0.11.2",
            "0.12.0": "vllm/vllm-openai:v0.12.0",
            "0.13.0": "vllm/vllm-openai:v0.13.0",
            "0.14.0": "vllm/vllm-openai:v0.14.0-cu130",
            "0.14.1": "vllm/vllm-openai:v0.14.1-cu130",
            "0.15.0": "vllm/vllm-openai:v0.15.0-cu130",
            "0.15.1": "vllm/vllm-openai:v0.15.1-cu130",
            "0.16.0": "vllm/vllm-openai:v0.16.0-cu130",
            "0.17.0": "vllm/vllm-openai:v0.17.0-cu130",
            "0.17.1": "vllm/vllm-openai:v0.17.1-cu130",
            "0.18.0": "vllm/vllm-openai:v0.18.0-cu130",
            "0.18.1": "vllm/vllm-openai:v0.18.1-cu130",
            "0.19.0": "vllm/vllm-openai:v0.19.0-cu130",
            "0.19.1": "vllm/vllm-openai:v0.19.1-cu130",
            "0.20.0": "vllm/vllm-openai:v0.20.0",
            "0.20.1": "vllm/vllm-openai:v0.20.1",
            "0.20.2": "vllm/vllm-openai:v0.20.2",
            "0.21.0": "vllm/vllm-openai:v0.21.0",
            "0.22.0": "vllm/vllm-openai:v0.22.0",
            "0.22.1": "vllm/vllm-openai:v0.22.1",
            "0.24.0": "vllm/vllm-openai:v0.24.0",
            "0.25.0": "vllm/vllm-openai:v0.25.0",
            "0.25.1": "vllm/vllm-openai:v0.25.1",
            "0.26.0": "vllm/vllm-openai:v0.26.0",
            "0.27.0": "vllm/vllm-openai:v0.27.0",
            "0.28.0": "vllm/vllm-openai:v0.28.0",
            "0.29.0": "vllm/vllm-openai:v0.29.0",
            "0.30.0": "vllm/vllm-openai:v0.30.0",
            "0.32.0": "vllm/vllm-openai:v0.32.0",
        }
        for version, image in expected.items():
            assert (
                image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="595.84", engine_version=version)
                == image
            )
        assert image_catalog.pick_image(engine="vllm", arch="nvidia", engine_version="0.10.2") is None
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="520.61.05", engine_version="0.6.6")
            is None
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="535.288.01", engine_version="0.6.6")
            == "vllm/vllm-openai:v0.6.6"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="595.84", engine_version="0.6.6")
            == "vllm/vllm-openai:v0.6.6"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="570.211", engine_version="0.10.2")
            == "vllm/vllm-openai:v0.10.2"
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="575.64", engine_version="0.13.0")
            == "vllm/vllm-openai:v0.13.0"
        )

        # Releases with two CUDA lanes choose CUDA 12.9 below the native CUDA 13 floor.
        for version in (
            "0.14.0",
            "0.15.0",
            "0.17.0",
            "0.18.0",
            "0.19.0",
            "0.20.0",
            "0.20.1",
            "0.22.0",
            "0.25.0",
            "0.27.0",
            "0.32.0",
        ):
            expected_cu129 = f"vllm/vllm-openai:v{version}"
            if version in ("0.20.0", "0.20.1", "0.22.0", "0.25.0", "0.27.0", "0.32.0"):
                expected_cu129 += "-cu129"
            assert (
                image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="575.64", engine_version=version)
                == expected_cu129
            )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="569.99", engine_version="0.9.2")
            is None
        )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="574.99", engine_version="0.11.1")
            is None
        )

        # New image versions have an explicit API vocabulary, so the adapter won't assume latest flags.
        for version in ("0.9.1", "0.9.2", "0.10.0", "0.10.1", "0.10.1.1"):
            vocab = image_catalog.engine_vocab("vllm", version)
            assert vocab is not None and vocab.pooling_flags == "task" and vocab.multinode is False
        vocab = image_catalog.engine_vocab("vllm", "0.11.1")
        assert vocab is not None and vocab.pooling_flags == "runner_convert" and vocab.multinode is False
        vocab = image_catalog.engine_vocab("vllm", "0.6.6")
        assert vocab is not None
        assert vocab.pooling_flags == "task" and vocab.multinode is False
        assert vocab.tool_parsers == ["hermes"] and vocab.reasoning_parsers == []
        for version in (
            "0.14.0",
            "0.15.0",
            "0.17.0",
            "0.18.0",
            "0.19.0",
            "0.20.0",
            "0.20.1",
            "0.22.0",
            "0.25.0",
            "0.27.0",
            "0.32.0",
        ):
            vocab = image_catalog.engine_vocab("vllm", version)
            assert vocab is not None and vocab.pooling_flags == "runner_convert" and vocab.multinode is True

    def test_vllm_cuda129_forward_compat_expands_supported_driver_branches(self):
        versions = (
            "0.17.0",
            "0.17.1",
            "0.18.0",
            "0.18.1",
            "0.19.0",
            "0.19.1",
            "0.20.0",
            "0.20.1",
            "0.20.2",
            "0.21.0",
            "0.22.0",
            "0.22.1",
            "0.23.0",
            "0.24.0",
            "0.25.0",
            "0.25.1",
            "0.26.0",
            "0.27.0",
            "0.27.1",
            "0.28.0",
            "0.29.0",
            "0.30.0",
            "0.32.0",
        )
        for version in versions:
            image = f"vllm/vllm-openai:v{version}"
            if tuple(int(part) for part in version.split(".")) >= (0, 20, 0):
                image += "-cu129"
            for driver in ("535.288.01", "570.211.01"):
                entries = image_catalog.resolve_images(
                    engine="vllm",
                    arch="nvidia",
                    gpu_model="NVIDIA A100-SXM4-80GB",
                    driver_version=driver,
                    engine_version=version,
                )
                compat = [e for e in entries if e.params.get("env", {}).get("VLLM_ENABLE_CUDA_COMPATIBILITY") == "1"]
                assert any(e.image == image for e in compat), (version, driver, [e.image for e in compat])
                assert all(e.gpu_model_required and e.driver_required for e in compat)
            assert (
                image_catalog.pick_image(
                    engine="vllm",
                    arch="nvidia",
                    gpu_model="NVIDIA GeForce RTX 2080 Ti",
                    driver_version="535.288.01",
                    engine_version=version,
                )
                is None
            )
        assert (
            image_catalog.pick_image(engine="vllm", arch="nvidia", driver_version="535.288.01", engine_version="0.20.0")
            is None
        )

    def test_added_vllm_cuda13_versions_use_official_compatibility_gates(self):
        for version in ("0.17.0", "0.18.0", "0.19.0", "0.20.0", "0.20.1"):
            tag = f"vllm/vllm-openai:v{version}"
            if version not in ("0.20.0", "0.20.1"):
                tag += "-cu130"
            for driver in ("535.288.01", "570.211.01"):
                entry = image_catalog.pick_entry(
                    engine="vllm",
                    arch="nvidia",
                    gpu_model="NVIDIA A100-SXM4-80GB",
                    driver_version=driver,
                    engine_version=version,
                )
                assert entry is not None and entry.image == tag
                assert entry.params["env"]["VLLM_ENABLE_CUDA_COMPATIBILITY"] == "1"
                assert entry.gpu_model_required and entry.driver_required
            assert (
                image_catalog.pick_image(
                    engine="vllm",
                    arch="nvidia",
                    gpu_model="NVIDIA GeForce RTX 2080 Ti",
                    driver_version="535.288.01",
                    engine_version=version,
                )
                is None
            )

    def test_vllm_cuda_forward_compat_is_limited_to_known_datacenter_gpus(self):
        a100 = image_catalog.pick_entry(
            engine="vllm",
            arch="nvidia",
            gpu_model="NVIDIA A100-SXM4-80GB",
            driver_version="535.288.01",
            engine_version="0.17.1",
        )
        assert a100 is not None
        assert a100.image == "vllm/vllm-openai:v0.17.1-cu130"
        assert a100.params["env"]["VLLM_ENABLE_CUDA_COMPATIBILITY"] == "1"
        assert a100.gpu_model_required and a100.driver_required
        a2 = image_catalog.pick_entry(
            engine="vllm",
            arch="nvidia",
            gpu_model="NVIDIA A2",
            driver_version="535.288.01",
            engine_version="0.17.1",
        )
        assert a2 is not None and a2.image == "vllm/vllm-openai:v0.17.1-cu130"
        a16 = image_catalog.pick_entry(
            engine="vllm",
            arch="nvidia",
            gpu_model="NVIDIA A16",
            driver_version="570.211.01",
            engine_version="0.18.1",
        )
        assert a16 is not None and a16.image == "vllm/vllm-openai:v0.18.1-cu130"
        ax800 = image_catalog.pick_entry(
            engine="vllm",
            arch="nvidia",
            gpu_model="NVIDIA AX800",
            driver_version="535.288.01",
            engine_version="0.17.1",
        )
        assert ax800 is not None and ax800.image == "vllm/vllm-openai:v0.17.1-cu130"
        assert (
            image_catalog.pick_image(
                engine="vllm",
                arch="nvidia",
                gpu_model="NVIDIA A100-SXM4-80GB",
                driver_version="535.288.01",
            )
            == "vllm/vllm-openai:v0.27.1"
        )

        assert (
            image_catalog.pick_image(
                engine="vllm",
                arch="nvidia",
                gpu_model="NVIDIA GeForce RTX 2080 Ti",
                driver_version="535.288.01",
                engine_version="0.17.1",
            )
            is None
        )
        unknown_driver_entry = image_catalog.pick_entry(
            engine="vllm",
            arch="nvidia",
            gpu_model="NVIDIA A100-SXM4-80GB",
            engine_version="0.17.1",
        )
        # Unknown driver cannot pick either a native CUDA 13 or forward-compat image.
        assert unknown_driver_entry is None
        assert (
            image_catalog.pick_image(
                engine="vllm",
                arch="nvidia",
                gpu_model="NVIDIA B200",
                driver_version="535.288.01",
                engine_version="0.30.0",
            )
            is None
        )
        b200 = image_catalog.pick_entry(
            engine="vllm",
            arch="nvidia",
            gpu_model="NVIDIA B200",
            driver_version="570.211.01",
            engine_version="0.30.0",
        )
        assert b200 is not None and b200.params["env"]["VLLM_ENABLE_CUDA_COMPATIBILITY"] == "1"
        assert (
            image_catalog.pick_image(
                engine="vllm",
                arch="nvidia",
                gpu_model="NVIDIA GB300",
                driver_version="570.211.01",
                engine_version="0.30.0",
            )
            is None
        )
        assert all(
            not entry.gpu_model_required
            for entry in list_entries(engine="vllm", engine_version="0.17.1", driver_version="535.288.01")
        )

    def test_expanded_official_sglang_versions_and_vocab_gates(self):
        expected = {
            "0.5.5.post3": ("580.173", "lmsysorg/sglang:v0.5.5.post3"),
            "0.5.7": ("575.64", "lmsysorg/sglang:v0.5.7"),
            "0.5.8.post1": ("580.173", "lmsysorg/sglang:v0.5.8.post1-cu130"),
            "0.5.10": ("575.64", "lmsysorg/sglang:v0.5.10"),
            "0.5.11": ("580.173", "lmsysorg/sglang:v0.5.11"),
            "0.5.12.post1": ("580.173", "lmsysorg/sglang:v0.5.12.post1"),
            "0.5.13.post1": ("580.173", "lmsysorg/sglang:v0.5.13.post1"),
            "0.5.15.post1": ("580.173", "lmsysorg/sglang:v0.5.15.post1"),
            "0.5.16": ("580.173", "lmsysorg/sglang:v0.5.16"),
            "0.5.18": ("580.173", "lmsysorg/sglang:v0.5.18"),
            "0.5.19": ("580.173", "lmsysorg/sglang:v0.5.19"),
            "0.5.20": ("580.173", "lmsysorg/sglang:v0.5.20"),
        }
        for version, (driver, image) in expected.items():
            assert (
                image_catalog.pick_image(
                    engine="sglang",
                    arch="nvidia",
                    gpu_model="NVIDIA A100-80G",
                    driver_version=driver,
                    engine_version=version,
                )
                == image
            )
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA A100-80G",
                driver_version="575.64",
                engine_version="0.5.12.post1",
            )
            == "lmsysorg/sglang:v0.5.12.post1-cu129"
        )
        assert image_catalog.engine_vocab("sglang", "0.5.11").parser_auto is False
        assert image_catalog.engine_vocab("sglang", "0.5.12").parser_auto is True
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA GeForce RTX 2080 Ti",
                driver_version="595.84",
                engine_version="0.5.20",
            )
            is None
        )

    def test_new_sglang_tags_match_cuda_channel_and_legacy_parser_vocab(self):
        expected = {
            "0.4.8": ("560.35", "lmsysorg/sglang:v0.4.8-cu126"),
            "0.4.9.post6": ("560.35", "lmsysorg/sglang:v0.4.9.post6-cu126"),
            "0.4.10.post2": ("560.35", "lmsysorg/sglang:v0.4.10.post2-cu126"),
            "0.5.1.post3": ("560.35", "lmsysorg/sglang:v0.5.1.post3-cu126"),
            "0.5.2": ("575.64", "lmsysorg/sglang:v0.5.2"),
            "0.5.3": ("575.64", "lmsysorg/sglang:v0.5.3-cu129"),
            "0.5.3.post3": ("575.64", "lmsysorg/sglang:v0.5.3.post3"),
            "0.5.4": ("575.64", "lmsysorg/sglang:v0.5.4"),
            "0.5.4.post3": ("575.64", "lmsysorg/sglang:v0.5.4.post3"),
            "0.5.10.post1": ("580.173", "lmsysorg/sglang:v0.5.10.post1-cu130"),
        }
        for version, (driver, image) in expected.items():
            assert (
                image_catalog.pick_image(
                    engine="sglang",
                    arch="nvidia",
                    gpu_model="NVIDIA A100-80G",
                    driver_version=driver,
                    engine_version=version,
                )
                == image
            )
            vocab = image_catalog.engine_vocab("sglang", version)
            assert vocab is not None and vocab.parser_auto is False

    def test_sglang_05_requires_known_supported_gpu_and_driver(self):
        for model in (
            "NVIDIA Tesla V100-SXM2-32GB",
            "NVIDIA Tesla P100-PCIE-16GB",
            "NVIDIA Tesla T4",
            "NVIDIA GeForce GTX 1080 Ti",
        ):
            assert (
                image_catalog.pick_image(
                    engine="sglang",
                    arch="nvidia",
                    gpu_model=model,
                    driver_version="595.84",
                    engine_version="0.5.20",
                )
                is None
            )

        assert (
            image_catalog.pick_image(engine="sglang", arch="nvidia", driver_version="595.84", engine_version="0.5.20")
            is None
        )
        assert (
            image_catalog.pick_image(
                engine="sglang", arch="nvidia", gpu_model="NVIDIA A100-80G", engine_version="0.5.20"
            )
            is None
        )

        # Matching legacy GPU names is case-insensitive; Ampere remains eligible.
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="nvidia tesla v100-sxm2-32gb",
                driver_version="595.84",
                engine_version="0.5.20",
            )
            is None
        )
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA A100-80G",
                driver_version="595.84",
                engine_version="0.5.20",
            )
            == "lmsysorg/sglang:v0.5.20"
        )

    def test_llamacpp_version_is_exact_and_never_falls_back_to_family_tag(self):
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="595.84", engine_version="b10975")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda13-b10975"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="595.84", engine_version="b10991")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda13-b10991"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="595.84", engine_version="b11243")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda13-b11243"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="580.00", engine_version="b11243")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda13-b11243"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="575.64", engine_version="b10975")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda12-b10975"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="575.64", engine_version="b10991")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda12-b10991"
        )
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="575.64", engine_version="b11243")
            == "ghcr.io/ggml-org/llama.cpp:server-cuda12-b11243"
        )
        assert image_catalog.pick_image(engine="llamacpp", arch="nvidia", engine_version="b11243") is None
        assert (
            image_catalog.pick_image(engine="llamacpp", arch="nvidia", driver_version="595.84", engine_version="b00000")
            is None
        )

    def test_added_sglang_versions_and_turing_exclusion(self):
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA A100-80G",
                driver_version="595.84",
                engine_version="0.5.9",
            )
            == "lmsysorg/sglang:v0.5.9-cu130"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA A100-80G",
                driver_version="595.84",
                engine_version="0.5.14",
            )
            == "lmsysorg/sglang:v0.5.14-cu129"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA GeForce RTX 2080 Ti",
                driver_version="595.84",
                engine_version="0.5.14",
            )
            is None
        )

        # 未显式选 0.5.x 时 Turing 仍应回落到已实测的 0.4.10。
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA GeForce RTX 2080 Ti",
                driver_version="595.84",
            )
            == "lmsysorg/sglang:v0.4.10-cu126"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                gpu_model="NVIDIA T4",
                driver_version="595.84",
            )
            == "lmsysorg/sglang:v0.4.10-cu126"
        )

    def test_sglang_0300_uses_official_tested_image_and_legacy_vocab(self):
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                driver_version="595.84",
                engine_version="0.3.0",
            )
            == "lmsysorg/sglang:v0.3.0-cu118"
        )
        assert (
            image_catalog.pick_image(
                engine="sglang",
                arch="nvidia",
                driver_version="520.61",
                engine_version="0.3.0",
            )
            is None
        )
        vocab = image_catalog.engine_vocab("sglang", "0.3.0")
        assert vocab is not None
        assert not vocab.parser_auto and not vocab.metrics_flag
        assert not vocab.pp and not vocab.multinode

    def test_unknown_engine_returns_empty(self):
        assert image_catalog.pick_image(engine="tensorrt", arch="nvidia") is None

    def test_list_by_engine_version(self):
        items = list_entries(engine="vllm", engine_version="0.23.0")
        assert {e.id for e in items} == {
            "vllm-nvidia-default",
            "vllm-ascend-910b",
            "vllm-ascend-910c",
            "vllm-ascend-950dt",
            "vllm-ascend-310p-0230",
        }

    def test_list_entries_versions_newest_first(self):
        """镜像版本下拉按返回顺序渲染——同组内版本必须从新到旧，无版本条目排在组尾。"""
        items = list_entries(engine="vllm", arch="nvidia")
        versions = [e.engine_version for e in items if e.engine_version]
        assert versions == sorted(versions, key=image_catalog._ver_key, reverse=True)
        unversioned_at = next((i for i, e in enumerate(items) if not e.engine_version), len(items))
        assert all(e.engine_version for e in items[:unversioned_at])


class TestMergeStartArgs:
    def test_image_floor_system_override_user_override(self):
        # image: --dtype half ; system: --port 8000 --model /m ; user: --dtype float16 --trust-remote-code
        out = merge_start_args(
            ["--dtype", "half"],
            ["--port", "8000", "--model", "/m"],
            ["--dtype", "float16", "--trust-remote-code"],
            protected={"--port", "--model"},
        )
        # system 必控保住；user 覆盖 --dtype；image 补的 trust-remote-code 也进
        assert out.index("--port") < out.index("--model")
        assert "float16" in out
        assert "--trust-remote-code" in out
        assert "half" not in out  # image 的 --dtype half 被 user 覆盖

    def test_protected_immune_to_user(self):
        # user 试图改 --port（protected）→ 系统值保留
        out = merge_start_args([], ["--port", "8000"], ["--port", "9000"], protected={"--port"})
        assert "9000" not in out
        assert "8000" in out

    def test_image_floor_only(self):
        # system 空，image 补 --dtype half
        out = merge_start_args(["--dtype", "half"], [], [], protected=set())
        assert out == ["--dtype", "half"]

    def test_override_replaces_value_in_place(self):
        """用户层同名 flag 整体替换且保持在首次出现的位置，新 flag 追加末尾。"""
        out = merge_start_args([], ["--x", "1", "--y", "2"], ["--x", "9"], protected=set())
        assert out == ["--x", "9", "--y", "2"]


class TestAssembleEnv:
    def test_three_layer_env(self):
        from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter
        from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology, EngineConfig

        entry = ImageEntry(
            id="t", engine="vllm", arch="nvidia", image="x", params={"env": {"HF_HUB_OFFLINE": "1", "IMG_ONLY": "1"}}
        )
        inst = InstanceSpec(instance_id="i", assigned_gpus=[0], model_path="/m", served_model_name="m", port=8000)
        env = VLLMAdapter().assemble_env(inst, Topology(tp=1), EngineConfig(), entry, {"env": {"USER_VAR": "u"}})
        assert env["HF_HUB_OFFLINE"] == "1"  # image floor
        assert env["IMG_ONLY"] == "1"  # image floor（未被 system 覆盖）
        assert "USER_VAR" in env and env["USER_VAR"] == "u"  # user 最高


class TestExplainUnresolved:
    """resolve_image_ref 未命中时的人读诊断（inspect 降级提示用）。"""

    def test_states_target_driver_and_current(self):
        """驱动不满足 → 提示含真实镜像 + 目标驱动约束 + 当前驱动 + 行动建议。"""
        msg = image_catalog.explain_unresolved(
            engine="vllm",
            arch="nvidia",
            gpu_model="A100-80G",
            driver_version="550.54",
            engine_version="0.26.0",
        )
        assert "vllm/vllm-openai:v0.26.0-cu129" in msg  # 真实镜像摆出来
        assert ">=575" in msg  # 目标驱动版本
        assert "550.54" in msg  # 当前驱动
        assert "升级驱动" in msg or "换 image_tag" in msg

    def test_no_such_version(self):
        """版本不存在 → 明确说未找到该版本镜像。"""
        msg = image_catalog.explain_unresolved(
            engine="vllm",
            arch="nvidia",
            driver_version="550",
            engine_version="9.9.9",
        )
        assert "9.9.9" in msg and "未找到" in msg


class TestCatalogConsistency:
    """catalog 是运维可改的 JSON，写错架构会产出「选了必然被部署拒绝」的死条目。"""

    def test_entry_engine_and_arch_are_declared(self):
        """每条画像的引擎必须已注册，架构必须是该引擎 adapter 声明支持的。

        两个来源各说各话时的表现：条目在管理端镜像列表里正常展示，用户选了要到部署才被拒
        （曾经的 sglang+cann——镜像录了 910B/910C，而 SGLangAdapter 只声明 nvidia，
        build_device_config 对 cann 根本没实现）。
        """
        from llmpod.apps.manager.deployment.engine import get_adapter, registered_engines

        image_catalog.reload()
        known_engines = set(registered_engines())
        dead: list[str] = []
        for entry in list_entries():
            if entry.engine not in known_engines:
                dead.append(f"{entry.id}: 引擎 {entry.engine} 未注册")
            elif entry.arch not in get_adapter(entry.engine).supported_gpu_archs:
                dead.append(f"{entry.id}: {entry.engine}+{entry.arch} 未被该引擎声明支持")
        assert dead == [], f"镜像目录与引擎能力声明不一致：{dead}"
