"""协议层抽象：抽象基类 + 转发结果类型 + 共享工具。

每个协议（openai chat / embeddings / anthropic messages / rerank）实现一个
``ProtocolAdapter`` 子类，把协议专属逻辑收口到实现类：

- 转发参数：model 别名 → served_model、流式注入；
- 响应整形：model 回写别名、全量透传（不剔除上游字段）、SSE 重序列化；
- usage/文本提取：从非流式 body 与流式逐事件中抽出 usage 增量与文本增量，
  产出到 ``ForwardResult`` / ``StreamEvent``，路由层零协议知识。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import httpx

from llmpod.apps.proxyserver.snapshot import RouteTarget
from llmpod.config.settings import settings
from llmpod.core.encoding import lenient_utf8

# 客户端入站头中需要原样转交给上游的 allowlist。anthropic-version / anthropic-beta
# 是客户端与上游的能力协商（prompt caching、1M 上下文等 beta），网关替客户端定死
# 版本会让这些能力静默失效、长上下文请求可能被上游 400。鉴权等其余头不透传。
PASSTHROUGH_HEADERS: tuple[str, ...] = ("anthropic-version", "anthropic-beta")


@dataclass
class StreamEvent:
    """流式响应中的一个事件：待转发的 SSE 帧 + 本事件携带的 usage/文本增量。"""

    wire: bytes  # 待转发的 SSE 帧（含换行）
    usage: dict | None = None  # 本事件 usage 增量（合并由 inference_handler 层做）
    text: str | None = None  # 本事件文本增量（日志还原 body 用）
    billable_text: str | None = None  # 非展示文本（工具参数/思考内容）也须计入断流估算
    upstream_error: str | None = None  # SSE 流内错误（HTTP 状态已是 200）
    # 结束原因（openai ``finish_reason`` / anthropic ``stop_reason``），原样保留不归一：
    # 只有收尾帧带值，取值翻译（哪个值算「被输出上限截断」）由查询侧一处负责。
    finish_reason: str | None = None


@dataclass
class ForwardResult:
    """一次转发的完整结果：非流式 body 或流式事件迭代器，二选一。"""

    body: dict | None = None  # 非流式整形后响应体
    stream: AsyncIterator[StreamEvent] | None = None
    usage: dict | None = None  # 非流式 usage（= body 内 usage）
    finish_reason: str | None = None  # 非流式结束原因（流式逐事件带）


def collect_passthrough_headers(headers) -> dict[str, str]:
    """从入站请求头挑出 allowlist 内的头（PASSTHROUGH_HEADERS），供转发携带。

    返回空 dict 表示客户端没带任何协商头，适配器按协议缺省值出。重复头（如多个
    anthropic-beta）只取第一个——协商头带多值属罕见形态，不为此引入多值语义。
    """
    out: dict[str, str] = {}
    for name in PASSTHROUGH_HEADERS:
        value = headers.get(name)
        if value:
            out[name] = value
    return out


def json_payload(body: dict) -> bytes:
    """转发请求体序列化，配合 ``content=`` 使用（编码经 :func:`lenient_utf8`）。

    httpx 的 ``json=`` 走严格 utf-8 且不带 sort_keys：客户端 JSON 里的孤立代理项
    （``\\ud800`` 转义）解析回 str 后再交给它编码会抛 UnicodeEncodeError——坏输入
    在转发层被打成裸 500。键序保持客户端原样（排序是指纹口径，不是线上口径）。
    """
    return lenient_utf8(json.dumps(body, ensure_ascii=False, separators=(",", ":")))


def normalize_api_base(api_base: str, version_prefix: str = "/v1") -> str:
    """转发基址归一化：裸 host:port 补版本根，已含版本根或自定义路径不动。

    编排侧写的引擎 api_base 是裸 host:port（无 /v1）；外部登记端点习惯带
    https://api.openai.com/v1。两种写法归一到「带版本根」形态，供
    :func:`resolve_url` 在端点**未登记主路径**时接协议主路径
    （/chat/completions、/messages、/rerank）。
    自定义路径（反向代理 /api 等）视为已归一，原样保留。
    """
    parts = urlsplit(api_base)
    path = parts.path.rstrip("/")
    if path == "" or path == version_prefix:
        path = version_prefix
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def bearer_headers(api_key: str | None) -> dict:
    """openai 系鉴权头（openai chat/embeddings、rerank 供应商均走 Bearer）。

    自带 content-type：本模块的请求体统一走 :func:`json_payload`（``content=`` 传参，
    httpx 不再自动设置），头构建器把 JSON 声明一并收口，调用处不必各自记得补。
    """
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def resolve_url(target: RouteTarget, default_path: str, sub_path: str = "") -> str:
    """统一转发 URL 策略：端点登记的主路径优先；未登记才归一化 api_base 后接协议主路径。

    ``default_path`` 是**端点未登记主路径时**的协议缺省主路径（/chat/completions、
    /messages、/rerank）。它不是「任意后缀」——端点登记了主路径时该参数被完全忽略，
    传什么都不会出现在结果里。

    登记的主路径（本地部署按 category×protocol 派生、外部登记端点可自定义）视为该
    端点的完整地址：api_base 尾斜杠与主路径前导斜杠均可省略，拼接时统一补齐单个 /。

    主路径之下的子路径（如 /messages/count_tokens）走 ``sub_path``，**不要**把它并进
    ``default_path``：那样在端点登记了主路径的分支里会被整个丢掉，请求落到主端点
    （对 /messages 而言就是真跑一次生成）。斜杠归一由本函数统一收口。
    """
    if target.path:
        base = target.api_base.rstrip("/") + "/" + target.path.lstrip("/")
    else:
        base = normalize_api_base(target.api_base).rstrip("/") + default_path
    if not sub_path:
        return base
    return base.rstrip("/") + "/" + sub_path.lstrip("/")


async def eager_stream(gen: AsyncIterator[StreamEvent]) -> ForwardResult:
    """eager 取首事件：在 forward 返回前建立上游连接并校验响应头。

    流式契约的机制实现——连接期错误（DNS/拒连/上游 4xx 5xx）在此同步抛出，
    调用方的 failover 循环才能捕获并切换端点；首事件回填到迭代器头部，
    调用方语义不变。流式适配器统一经此包装，不各写一份。

    上游返回 200 但流为空（anthropic 无 [DONE] 尾哨兵）时，首个 ``__anext__`` 抛
    ``StopAsyncIteration``——退化为空流交由下游正常收尾（结算按 usage=None 全额退、
    空 body 返回），不升级成 500。
    """
    it = gen.__aiter__()
    try:
        first = await it.__anext__()
    except StopAsyncIteration:
        return ForwardResult(stream=_empty_stream())
    return ForwardResult(stream=_chain(first, it))


async def _empty_stream() -> AsyncIterator[StreamEvent]:
    """产出零事件的异步迭代器（eager_stream 空流分支复用）。"""
    if False:  # 使函数成为异步生成器（产出零事件）
        yield StreamEvent(wire=b"")


async def _chain(first: StreamEvent, it: AsyncIterator[StreamEvent]) -> AsyncIterator[StreamEvent]:
    try:
        yield first
        async for ev in it:
            yield ev
    finally:
        close = getattr(it, "aclose", None)
        if close is not None:
            await close()


class ProtocolAdapter(ABC):
    """协议适配器抽象基类。

    子类需实现：

    - ``protocol``：落账 request_log.protocol 字段（openai / anthropic / ...）
    - ``upstream_errors``：路由层捕获的上游异常族（进冷却 + 错误归一）
    - ``forward()``：转发 + 整形 + usage/文本提取，产出 ``ForwardResult``

    客户端复用（httpx 按 origin 池化）与 URL 解析已下沉到基类：
    ``get_client`` 默认返回进程级共享连接池，子类按需覆盖（如注入独立池的测试）；
    ``_url(target, default_path, sub_path)`` 解析主端点地址（登记主路径优先，否则归一化
    后接协议主路径），主路径之下的子端点由 ``sub_path`` 追加，见 :func:`resolve_url`。

    流式契约：``forward()`` 流式分支必须经 ``eager_stream`` 包装生成器——
    连接期错误在调用方同步抛出，failover 循环才能捕获切换；进入迭代后的
    错误由调用方收尾逻辑兜底。
    """

    protocol: str = ""
    # 上游失败通用异常族（进冷却 + 错误归一）：连接/状态错 + JSON 解析失败。
    # 子类有专属异常时用 `+ (Xxx,)` 扩展即可。
    upstream_errors: tuple[type[Exception], ...] = (
        httpx.HTTPStatusError,
        httpx.RequestError,
        json.JSONDecodeError,
    )
    # 是否写 request_log 计量行（无 usage/计费语义的操作可关，如 count_tokens；
    # 关了也只关日志，计费走 held 独立判断——两者不是一回事）
    records_logs: bool = True

    @classmethod
    def get_client(cls, api_base: str | None = None, api_key: str | None = None):
        """默认共享连接池（httpx 内部按 origin 池化，跨端点复用）。"""
        return get_shared_client()

    def _url(self, target: RouteTarget, default_path: str, sub_path: str = "") -> str:
        """主端点地址；``sub_path`` 给主路径之下的子端点，拼接规则见 :func:`resolve_url`。"""
        return resolve_url(target, default_path, sub_path)

    @abstractmethod
    async def forward(
        self,
        client,
        target: RouteTarget,
        *,
        alias_name: str,
        payload: dict,
        passthrough_headers: dict[str, str] | None = None,
    ) -> ForwardResult:
        """转发请求并整形响应；流式/非流式由 payload 决定，二选一产出。

        ``passthrough_headers`` 是客户端入站头中的协商头子集（见
        :data:`PASSTHROUGH_HEADERS`，anthropic 系版本 / beta 头）；openai 系协议无
        对应语义，实现方可忽略该参数。
        """
        raise NotImplementedError


_shared_client = None


def get_shared_client():
    """进程级共享 httpx 客户端（懒建；关闭后重建）；lifespan 收尾统一 aclose。

    超时四段取自 settings（见 upstream_*_timeout）：引擎不可达 → 连接超时快速失败；
    引擎「活着不回包」由更外层的首字节墙钟上限兜（inference_handler 的
    ``_forward_bounded``，httpx 本身没有独立的 TTFB 超时）。
    """
    global _shared_client

    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout,
                read=settings.upstream_read_timeout,
                write=settings.upstream_write_timeout,
                pool=settings.upstream_pool_timeout,
            ),
        )
    return _shared_client


async def close_shared_clients() -> None:
    """关闭共享 httpx 连接池（进程退出前调用）。"""
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
