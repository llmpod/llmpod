"""会话前缀哈希（协议感知）：同一会话的连续请求算出稳定指纹。

规则：
1. 规范化 JSON：解析后 sort_keys + 稳定分隔符重序列化再哈希（语义相同即同哈希）；
2. 有效前缀按协议：OpenAI chat = tools + messages[:-1]（system 在数组头自然包含）；
   Anthropic = 顶层 system + tools + messages[:-1]；
3. 二进制降噪：base64 图片等长值替换为占位符后再参与规范化（哈希=存储内容的纯度）；
4. 垫场消息（开场的 system / developer）不产出边界：它们跨会话几乎一字不差，若
   参与计数，「只有一段系统提示词」也会成为可命中边界，共享同一段提示词的无关
   会话会被整体吸到同一端点；它们的身份进链种子，不进边界链；
5. embeddings / rerank（及 count_tokens）无会话语义 → None。

对外两个入口，分工不同：
- ``compute_conv_prefix_hash``：整段前缀一次算，落账列的历史口径；
- ``compute_boundary_hashes``：逐消息边界的链式序列，供路由侧做前缀亲和匹配。
"""

from __future__ import annotations

import hashlib
import re

from llmpod.core.encoding import canonical_json, lenient_utf8

_NO_SESSION_OPERATIONS = {"embeddings", "rerank", "anthropic.count_tokens"}

# 开场垫场角色：跨会话几乎一字不差（同产品共用同一段系统提示词），单独成为边界
# 会把无关会话吸到同一端点，故只允许它们参与链身份、不产出可命中边界
_PREAMBLE_ROLES = frozenset({"system", "developer"})

# base64 data URL（图片等多媒体内嵌）；>256 字符才视为二进制降噪目标，避免误伤短文本
_DATA_URL_RE = re.compile(r"data:[^;]+;base64,[A-Za-z0-9+/=]{256,}")
_PLACEHOLDER = "[image: …omitted]"


def _denoise(obj):
    """递归降噪：长 base64 data URL → 占位符（图片体积不参与指纹）。"""
    if isinstance(obj, str):
        return _DATA_URL_RE.sub(_PLACEHOLDER, obj)
    if isinstance(obj, list):
        return [_denoise(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _denoise(v) for k, v in obj.items()}
    return obj


def _preamble(messages: list) -> list:
    """messages 头部连续的垫场消息（system / developer 角色）。"""
    out: list = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") in _PREAMBLE_ROLES:
            out.append(message)
        else:
            break
    return out


def _protocol_head(operation: str, payload: dict) -> dict:
    """协议前缀里位于消息之前的部分（整段指纹与链式种子的共同头部）。

    Anthropic 的 system 在顶层，属于头部；OpenAI 的 system / developer 是 messages
    头部的垫场消息，不属于此（它们的身份由链种子的 ``preamble`` 承担）。``tools``
    是两者的共同项。头部字段清单是「什么算会话身份」的唯一定义处，两处口径必须同源。
    """
    head = {"tools": payload.get("tools") or []}
    if operation == "anthropic.messages":
        head["system"] = payload.get("system")
    return head


def compute_conv_prefix_hash(operation: str, payload: dict) -> str | None:
    """计算会话前缀指纹；无会话语义操作返回 None。"""
    if operation in _NO_SESSION_OPERATIONS:
        return None

    messages = payload.get("messages") or []
    prefix = {**_protocol_head(operation, payload), "messages": messages[:-1]}
    return hashlib.sha256(lenient_utf8(canonical_json(_denoise(prefix)))).hexdigest()


def _chain_seed(operation: str, payload: dict, preamble: list) -> str:
    """链式边界的种子：协议前缀里位于消息之前的部分（``preamble`` 由调用方算好传入）。

    Anthropic 的 system 在顶层，必须显式入种子——它不随消息累积，漏掉会让两个
    只有 system 不同的会话算出同一条链。OpenAI 的 system / developer 是 messages
    头部的垫场消息，同理必须入种子：它们不产出边界（见 ``compute_boundary_hashes``），
    身份若再不进种子，共享同一段系统提示词的不同会话就会从第一个非垫场边界起
    算成同一条链。
    """
    head = _protocol_head(operation, payload)
    if preamble:
        head["preamble"] = preamble
    return canonical_json(_denoise(head))


def compute_boundary_hashes(operation: str, payload: dict) -> list[str] | None:
    """会话前缀的链式边界 hash：下标 k 对应「已累积前 k+1 条有效消息」的边界。

    链式（``h_k = H(h_{k-1} ‖ 第 k 条消息)``）而非逐边界独立重算，两条理由：

    - 复杂度是 O(总字节)，逐边界重算是 O(消息数 × 前缀长度)；
    - hash 只由累积内容决定、与切法无关，同一前缀无论客户端怎么分块都算出同一个值。

    垫场消息（开场的 system / developer，见 ``_PREAMBLE_ROLES``）不计入边界：它们
    跨会话共享，「只累积了垫场」的前缀没有会话区分度，写进亲和表会让所有共享
    同一段系统提示词的全新会话被吸到同一个端点、绕过负载均衡。

    与 ``compute_conv_prefix_hash`` 的分工：那个取 ``messages[:-1]`` 整段一次算，
    是落账列的历史口径（有查询过滤依赖），定义不能动；本函数产出的是逐边界序列，
    供路由侧做「最长边界优先」匹配。无会话语义的操作返回 None。
    """
    if operation in _NO_SESSION_OPERATIONS:
        return None

    messages = payload.get("messages") or []
    preamble = _preamble(messages)
    effective = messages[len(preamble) :]
    acc = hashlib.sha256(lenient_utf8(_chain_seed(operation, payload, preamble))).hexdigest()
    out: list[str] = []
    for message in effective:
        acc = hashlib.sha256(lenient_utf8(acc + canonical_json(_denoise(message)))).hexdigest()
        out.append(acc)
    return out
