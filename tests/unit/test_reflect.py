"""反射工具单元测试。"""

from __future__ import annotations

from llmpod.tunnel.util.reflect import (
    is_async_gen,
    is_coro,
    class_metadata,
    build_params,
)


class TestIsAsyncGen:
    def test_async_generator_function(self):
        async def gen():
            yield 1

        assert is_async_gen(gen) is True

    def test_coroutine_function(self):
        async def coro():
            return 1

        assert is_async_gen(coro) is False

    def test_sync_function(self):
        def sync():
            return 1

        assert is_async_gen(sync) is False


class TestIsCoro:
    def test_coroutine_function(self):
        async def coro():
            return 1

        assert is_coro(coro) is True

    def test_sync_function(self):
        def sync():
            return 1

        assert is_coro(sync) is False

    def test_async_gen(self):
        async def gen():
            yield 1

        assert is_coro(gen) is False


class TestClassMetadata:
    def test_reflects_public_methods(self):
        class MyService:
            def method_a(self, x: int) -> int:
                return x

            async def method_b(self, name: str) -> str:
                return name

            def _private(self):
                pass

        meta = class_metadata(MyService)
        assert "method_a" in meta
        assert "method_b" in meta
        assert "_private" not in meta  # 私有方法被排除

    def test_stream_detection(self):
        class MyService:
            async def streaming(self, n: int):
                for i in range(n):
                    yield i

            async def unary(self) -> str:
                return "ok"

        meta = class_metadata(MyService)
        assert meta["streaming"]["stream"] is True
        assert meta["unary"]["stream"] is False

    def test_param_names(self):
        class MyService:
            def add(self, a: int, b: int) -> int:
                return a + b

        meta = class_metadata(MyService)
        assert meta["add"]["params"] == ["a", "b"]

    def test_cached(self):
        """class_metadata 应按类缓存。"""

        class MyService:
            def m(self):
                pass

        meta1 = class_metadata(MyService)
        meta2 = class_metadata(MyService)
        assert meta1 is meta2  # 同一对象（已缓存）


class TestBuildParams:
    def test_positional_args(self):
        result = build_params(["a", "b"], (1, 2), {})
        assert result == {"a": 1, "b": 2}

    def test_kwargs_only(self):
        result = build_params(["a", "b"], (), {"a": 10, "b": 20})
        assert result == {"a": 10, "b": 20}

    def test_mixed_args_kwargs(self):
        result = build_params(["a", "b", "c"], (1,), {"b": 2, "c": 3})
        assert result == {"a": 1, "b": 2, "c": 3}

    def test_overflow_positional(self):
        result = build_params(["a"], (1, 2, 3), {})
        assert result == {"a": 1, "1": 2, "2": 3}

    def test_empty(self):
        result = build_params([], (), {})
        assert result == {}
