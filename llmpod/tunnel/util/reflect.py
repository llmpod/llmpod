"""反射工具: 方法元数据反射 + 参数映射。纯函数,无状态。

- class_metadata(cls): 反射(抽象)类为 {method_name: {name, stream, params}}。
  按类缓存(反射 + get_type_hints 开销较大)。
- build_params(param_names, args, kwargs): 将位置参数映射为命名参数,用于 JSON-RPC。
"""

from __future__ import annotations

import collections.abc
import functools
import inspect
import typing

from pydantic import BaseModel


def _unwrap(fn):
    """解包 staticmethod/classmethod 装饰器,获取原始函数。"""
    fn = inspect.unwrap(fn)
    return getattr(fn, "__func__", fn)


def is_async_gen(fn) -> bool:
    """是否为异步生成器函数(流式方法)?"""
    return inspect.isasyncgenfunction(_unwrap(fn))


def is_coro(fn) -> bool:
    """是否为协程函数(async def 一元方法)?"""
    return inspect.iscoroutinefunction(_unwrap(fn))


def returns_stream(fn) -> bool:
    """此函数是否返回流: 异步生成器,或返回注解为 AsyncIterator/AsyncGenerator?"""
    if is_async_gen(fn):
        return True
    try:
        ret = typing.get_type_hints(fn).get("return")
    except Exception:
        ret = None
    if ret is None:
        return False
    origin = typing.get_origin(ret)
    if origin is None:
        return ret in (collections.abc.AsyncIterator, collections.abc.AsyncGenerator)
    try:
        return issubclass(origin, (collections.abc.AsyncIterator, collections.abc.AsyncGenerator))
    except TypeError:
        return False


@functools.cache
def class_metadata(cls) -> dict[str, dict]:
    """反射(抽象)类的方法元数据: {method_name: {name, stream, params, return}}。

    结果按类缓存(反射 + get_type_hints 开销较大)。
    ``return`` 为返回类型注解(供 proxy 自动 model_validate); 解析失败为 None。
    返回的字典仅供读取使用; 请勿修改。
    """
    meta: dict[str, dict] = {}
    for mname, fn in vars(cls).items():
        if mname.startswith("_") or not callable(fn):
            continue
        try:
            params = [p for p in inspect.signature(fn).parameters if p != "self"]
        except (ValueError, TypeError):
            params = []
        meta[mname] = {
            "name": mname,
            "stream": returns_stream(fn),
            "params": params,
            "return": _return_type(fn),
        }
    return meta


def _return_type(fn) -> type | None:
    """提取方法返回类型注解(get_type_hints 解析 forward ref); 失败返回 None。"""
    try:
        return typing.get_type_hints(fn).get("return")
    except Exception:
        return None


def build_params(param_names: list[str], args, kwargs) -> dict:
    """按参数名将位置参数映射 + 合并 kwargs 为 JSON-RPC params 对象。

    调用方(master 端 MethodProxy / HTTP 入口)使用此函数将 *args+**kwargs
    转换为命名参数对象; worker 端通过 fn(**params) 解包。
    """
    out = dict(kwargs)
    for i, a in enumerate(args):
        if i < len(param_names):
            out[param_names[i]] = a
        else:
            out[str(i)] = a  # overflow: fall back to positional index
    return out


@functools.cache
def _param_hints(fn) -> dict:
    """缓存: 函数的参数名→类型注解(去掉 return)。基于底层函数缓存(稳定 hash)。"""
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        return {}
    hints.pop("return", None)
    return hints


def coerce_wire(value, ann):
    """按注解把 wire dict/list 归一为 Pydantic model / model 列表（两个方向的单一实现）。

    master 出站（MethodProxy 按返回注解归一结果）与 worker 入参（``coerce_params``）
    共用本函数：

    - ``SomeModel``: dict → ``model_validate``
    - ``list/tuple/set/frozenset[SomeModel]``: 逐项归一（返回 list）
    - 其余（无注解/标量/bare list/元素非 BaseModel/形状不匹配）: 原样返回，向后兼容
    """
    if ann is None:
        return value
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        if isinstance(value, dict):
            return ann.model_validate(value)
        return value
    origin = typing.get_origin(ann)
    if origin in (list, tuple, set, frozenset):
        args = typing.get_args(ann) or (None,)
        item_ann = args[0]
        if isinstance(item_ann, type) and issubclass(item_ann, BaseModel) and isinstance(value, list):
            return [item_ann.model_validate(v) if isinstance(v, dict) else v for v in value]
    return value


def coerce_params(fn, params: dict) -> dict:
    """按方法签名注解,把 wire dict 参数自动 model_validate 为 Pydantic model。

    与 master 端 MethodProxy 出站归一(``coerce_wire``)对称: master 按返回注解归一结果,
    worker 按参数注解归一入参。归一逻辑共用 ``coerce_wire``。
    供 :meth:`ServiceRegistry.dispatch` 在调用本地方法前归一参数。
    """
    out = dict(params)
    hints = _param_hints(_unwrap(fn))
    for name, ann in hints.items():
        if name in out:
            out[name] = coerce_wire(out[name], ann)
    return out
