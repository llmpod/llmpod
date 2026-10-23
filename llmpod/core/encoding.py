"""宽容 utf-8 编码与规范化序列化：坏客户端输入不该在序列化边界炸成裸 500。

客户端 JSON 里的 ``\\ud800`` 转义会解析成孤立代理项（Python str 允许持有），严格
utf-8 在 ``.encode()`` 时抛 UnicodeEncodeError——指纹计算、落账块、上游转发、SSE
重序列化都在请求路径上，这类坏字符必须原样落盘（WTF-8）交下游按自己的口径处置，
而不是把请求打成 500 / 计量行写一半崩掉。ASCII 输入编码结果与严格模式逐字节相同。
"""

from __future__ import annotations

import json


def lenient_utf8(s: str) -> bytes:
    """utf-8 编码，容忍孤立代理项（见模块 docstring）。"""
    return s.encode("utf-8", "surrogatepass")


def canonical_json(obj) -> str:
    """规范化 JSON 序列化：键序稳定 + 紧凑分隔符 + 不转义非 ASCII。

    会话指纹的**唯一**序列化口径（整段指纹与逐消息链式边界共用）——同一份 payload
    必须算出同一个指纹，两处各写一份 ``json.dumps`` 参数迟早分叉。落成字节时由调用方
    再过 :func:`lenient_utf8`；这层只管「怎么序列化」，不管「怎么编码」。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
