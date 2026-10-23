"""引擎注册表。"""

from __future__ import annotations

from llmpod.apps.manager.deployment.engine.base import EngineAdapter
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter
from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter
from llmpod.core.exceptions import LLMPodValidationError

# engine 名 → 适配器类（sglang/llamacpp 为能力声明就绪的占位，参数生成 v1 未实现）
_REGISTRY: dict[str, type[EngineAdapter]] = {
    "vllm": VLLMAdapter,
    "sglang": SGLangAdapter,
    "llamacpp": LlamaCppAdapter,
}


def get_adapter(engine: str) -> EngineAdapter:
    """按引擎名取适配器实例（每次新建，无状态）。

    ``engine_name`` 由注册表注入（= 注册键）：它是 image_catalog 的引擎键，注册表已经是
    「引擎名」的唯一定义处，适配器再自报一个字符串就会漂移。
    """
    cls = _REGISTRY.get(engine)
    if cls is None:
        raise LLMPodValidationError(f"不支持的引擎: {engine}（已注册: {list(_REGISTRY)}）")
    adapter = cls()
    adapter.engine_name = engine
    return adapter


def registered_engines() -> list[str]:
    return list(_REGISTRY)


__all__ = [
    "EngineAdapter",
    "VLLMAdapter",
    "SGLangAdapter",
    "LlamaCppAdapter",
    "get_adapter",
    "registered_engines",
]
