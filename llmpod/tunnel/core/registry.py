"""服务注册表 (worker 端) — 核心层。

Worker 持有注册表。当 channel._handle_call 收到调用时,
通过反射按 service/method 分发到已注册的服务实例,
返回 ("value", v) 或 ("stream", async_gen)。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..util.reflect import coerce_params, is_async_gen, is_coro


class ServiceRegistry:
    """Worker 端本地服务注册表: 通过反射按 service/method 分发。"""

    def __init__(self):
        self._services: dict[str, Any] = {}

    def register(self, instance: Any, name: str | None = None) -> str:
        """注册服务实例。name 默认为类名。"""
        name = name or type(instance).__name__
        self._services[name] = instance
        return name

    def get_callable(self, service: str, method: str):
        """按 service 查找实例,再查找 method; 私有方法被禁止。"""
        inst = self._services.get(service)
        if inst is None:
            raise KeyError(f"service '{service}' not registered")
        fn = getattr(inst, method, None)
        if fn is None or method.startswith("_"):
            raise KeyError(f"method '{service}.{method}' not found")
        return fn

    async def dispatch(self, service: str, method: str, params: dict):
        """调用本地方法,返回 ("value", v) 或 ("stream", async_gen)。

        params 为命名参数对象(JSON-RPC 2.0 params); 调用前按方法签名注解把
        BaseModel 类型的 wire dict 入参自动 model_validate 为 model,通过 **params 解包。
        三个分支:
        - 异步生成器 → 流式(创建生成器,不等待)
        - 协程 → await
        - 同步 → to_thread 卸载到线程池
        """
        fn = self.get_callable(service, method)
        # 按方法签名注解把 wire dict 入参自动 model_validate 为 Pydantic model
        # (与 master 端 MethodProxy 按返回注解归一结果对称)。
        params = coerce_params(fn, params)
        if is_async_gen(fn):
            return "stream", fn(**params)
        if is_coro(fn):
            return "value", await fn(**params)
        # 同步方法卸载到线程池,避免阻塞事件循环。
        return "value", await asyncio.to_thread(fn, **params)
