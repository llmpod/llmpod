"""模型源探测器注册表（镜像 ``apps/manager/deployment/engine`` 的 ``get_adapter``）。

按 ``source`` 取探测器实例。未知 source → ``LLMPodValidationError``（硬错，区别于
「支持的源但探测失败」的软失败）。
"""

from __future__ import annotations

from llmpod.apps.manager.model_source.base import ModelFiles, ModelInspector
from llmpod.apps.manager.model_source.huggingface import HFHubInspector
from llmpod.apps.manager.model_source.local import LocalInspector
from llmpod.apps.manager.model_source.oss import OSSInspector
from llmpod.config.enums import ModelSource
from llmpod.core.exceptions import LLMPodValidationError

# source 名 → 探测器类（每次新建，无状态）
_REGISTRY: dict[ModelSource, type[ModelInspector]] = {
    ModelSource.HUGGINGFACE: HFHubInspector,
    ModelSource.OSS: OSSInspector,
    ModelSource.LOCAL: LocalInspector,
}


def get_inspector(source: str) -> ModelInspector:
    """按 source 取探测器实例。未知/未注册 source → LLMPodValidationError（422）。

    ``ModelSource`` 枚举含 ``HF`` 别名（未注册探测器），故 ``ModelSource(source)`` 成功
    不代表有探测器——再 ``_REGISTRY.get`` 兜一次。
    """
    try:
        key = ModelSource(source)
    except ValueError as e:
        raise LLMPodValidationError(f"不支持的模型来源: {source}（支持: {list(_REGISTRY)}）") from e
    cls = _REGISTRY.get(key)
    if cls is None:
        raise LLMPodValidationError(f"不支持的模型来源: {source}（支持: {list(_REGISTRY)}）")
    return cls()


def supported_sources() -> list[str]:
    return [s.value for s in _REGISTRY]


__all__ = [
    "ModelFiles",
    "ModelInspector",
    "get_inspector",
    "supported_sources",
    "HFHubInspector",
    "OSSInspector",
    "LocalInspector",
]
