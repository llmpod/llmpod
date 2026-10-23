"""来源端点连通性探测（管理端专用）：接入/编辑前置闸门 + /admin/endpoints/test 诊断。

管理端此前从不验证来源端点：登记外部端点时 ``health_status`` 无条件写死 healthy
（``services/endpoint_service.py``），全仓没有一处连过上游；而网关拿
``health_status != "healthy"`` 当候选闸门（``apps/proxyserver/snapshot.py:101``）。
状态不可信，这道准入闸门即形同虚设——Key 填错、地址写错的端点照样进池，直到真跑业务
流量才炸。本模块把「真的连一次」补上。

**判据只有一条**：最小 chat 请求回 2xx **且响应体是 JSON** = 通，其余（非 2xx / 2xx 但
不是 JSON / 连不上 / 超时）都算不通。JSON 判据与网关对齐——真实转发会 ``resp.json()``，
打错端口拿到 200+HTML（网关默认页/SPA 兜底）时照常放行就会是假健康。不做错误分类、
不翻译上游文案：不通时把上游原始回复（响应体原文 / 连接异常文案）原样
交给调用方，怎么表达由调用方决定（闸门 422、诊断接口 200 带结论）——在网关里再维护一层
会与上游文案漂移的翻译表，是上一版实现被删掉的原因。

**只探 chat**：``operations`` 仅含 embeddings / rerank 的端点（``CreateEndpointRequest``
允许）用 chat 探会拿到 4xx，属本口径下的**已知假阴性**——那种端点登记时用 ``skip_probe``
放行（或在全局开关关掉时登记）。刻意不做「按 operations 挑探测动作」：一个端点可能同时
声明多种操作，挑哪一个都是猜，而按协议查表会在适配器之外再养第二份操作→路径映射。

**地址与鉴权头都对齐网关**（``protocol.base`` 的 ``resolve_url`` / ``bearer_headers``）：
探的地址与头必须与真实流量走的、发的一模一样，否则「探 A 的结论写去 B 的健康」又是一版
假健康。适配器的 ``forward()`` 刻意不走——那是转发路径，牵着 model 别名回写与流式整形，
探测要的不是那些。也刻意不改网关任何文件（``resolve_url`` 的签名、anthropic 头的私有
实现都原样不动，那是别人的模块）：复用它现成的公共 API，手抄的那几行由测试对着**适配器
实际发出的请求**比对，抄错就红。

**探测失败只是「此刻不通」**：本模块不写任何库、不打任何标记——写回与否是路由层的决定
（见 ``routes/admin/endpoints.py`` 的 ``/test``）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

from llmpod.apps.proxyserver.protocol.base import bearer_headers, json_payload, resolve_url
from llmpod.apps.proxyserver.snapshot import RouteTarget
from llmpod.config.settings import settings
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models import ModelEndpoint

# 端点未登记主路径时的协议缺省主路径（登记了 path 时被 resolve_url 忽略）。
# 与网关适配器同值，由 tests/unit/test_endpoint_probe.py 的一致性守卫锁住。
_CHAT_PATH = {"openai": "/chat/completions", "anthropic": "/messages"}
# anthropic 版本缺省值：适配器里那份是 ``protocol/anthropic.py`` 的模块私有常量，管理端
# 不去把它公开化（那是别人的文件），手抄一份 + 守卫测试比对适配器实际发出的请求。
_ANTHROPIC_VERSION = "2023-06-01"
# 探测提示词：只要一个 token 的回应，内容本身无意义
_PROBE_PROMPT = "ping"
# 错误原文的截断口径：先按字节粗截再解码（错误信封都很短，而填错地址可能回整页 HTML，
# 没必要为读出 500 字而把几 MB 解成 str），再按字符收口到 _DETAIL_LIMIT。
# 字节数 = 字符上限 × 4，对任何 utf-8 输入都不少于 _DETAIL_LIMIT 字符，两步不会互相截短。
_DETAIL_LIMIT = 500
_DETAIL_BYTES = _DETAIL_LIMIT * 4
# 探测响应体的字节上限：流式收体、超限即断——非流式会把整个响应物化进内存，错配地址
# （文件服务/大 blob）在墙钟内可灌满管理端内存。最小 chat 应答（含错误信封）远小于此。
_PROBE_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProbeTarget:
    """一次探测的完整输入。

    值语义（``==`` 即「同一份连接配置」）是刻意的：闸门据此判定「这次编辑到底有没有动
    连接配置」，/test 据此判定「探的是不是该行存的那份配置」。字段清单只有这一份，
    「少比一个字段」在结构上不可能发生。
    """

    protocol: str
    api_base: str
    path: str | None
    served_model: str
    api_key: str | None

    @classmethod
    def from_stored(cls, endpoint: ModelEndpoint) -> ProbeTarget:
        """已存行的连接配置（探测的回落来源与同一性基准）。"""
        return cls(
            protocol=endpoint.protocol,
            api_base=endpoint.api_base,
            path=endpoint.path,
            served_model=endpoint.served_model,
            api_key=endpoint.api_key,
        )

    def matches(self, stored: ProbeTarget | None) -> bool:
        """是否就是那份已存配置（``stored=None`` = 没有可比对的行 → False）。

        同一性判定的**唯一**定义处：闸门据它决定「要不要探」，``/test`` 据它决定「要不要
        把结论写回该行」。两处各写一遍 ``stored is not None and target == stored`` 时，
        改口径（比如将来不比 ``path``）只改一处就会静默分叉。
        """
        return stored is not None and self == stored


@dataclass(frozen=True)
class EndpointProbeResult:
    """一次探测的结论（原样呈现给运维：地址、状态码、上游原文、耗时）。"""

    healthy: bool
    url: str  # 实际探测地址：运维一眼看出「探的不是我以为的那个地址」
    status_code: int | None = None  # 上游 HTTP 状态；连接/超时类失败为 None
    detail: str | None = None  # 上游响应体原文或连接异常文案；健康时为 None
    elapsed_ms: int = 0


def probe_applies(endpoint: ModelEndpoint) -> bool:
    """该行是否适用探测：本地部署端点不适用。

    本地端点的健康由部署状态投影独占（``services/deployment_projection.py``），探测结论
    会被投影覆盖——故既不探（update 直接跳过），也拒答（``/test`` 422：答了是误导）。
    规则只此一处：两个入口各写一遍 ``deployment_id is None`` 时，将来放宽口径只会改到一处。
    """
    return endpoint.deployment_id is None


def merge_target(
    *,
    stored: ProbeTarget | None = None,
    protocol: str | None = None,
    api_base: str | None = None,
    path: str | None = None,
    served_model: str | None = None,
    api_key: str | None = None,
) -> ProbeTarget:
    """请求体字段优先、缺省回落已存配置（「不传=不改」契约，同 ``update_endpoint``）。

    回落是必需的而非便利：编辑弹窗不回传已存 API Key（``ModelEndpointOut`` 不含该字段），
    不回落的编辑会拿空 Key 去探、必然 401，把「改个名字」也拦死。

    ``path`` / ``api_key`` 把空串也当「未提供」回落：客户端把未编辑的空输入序列化成 ""
    并不罕见，而 "" 会让 bearer_headers 掉头、让同一性比较误判「配置变了」——None 与 ""
    在这两处语义本就等价（resolve_url 对 ""/None 同样处理）。

    **存储的 Key 只发往存储的地址**：本次显式换了 ``api_base`` 且未自带 Key 时，不回落
    已存 Key——否则任何持 ENDPOINT_UPDATE 的调用方都能把别人的 Key 引到自己填的地址
    （该 Key 全仓唯一读路径就是这里，``ModelEndpointOut`` 不含它）。换地址试新 Key 就
    连 Key 一起传。
    """
    base = (
        stored
        if stored is not None
        else ProbeTarget(protocol="", api_base="", path=None, served_model="", api_key=None)
    )
    if api_base is not None and api_base != base.api_base and not api_key:
        merged_api_key = None  # 换地址不带走存储 Key：宁可探出 401（结论仍真实），不外发凭据
    else:
        merged_api_key = api_key if api_key else base.api_key
    return ProbeTarget(
        protocol=protocol if protocol is not None else base.protocol,
        api_base=api_base if api_base is not None else base.api_base,
        path=path if path else base.path,
        served_model=served_model if served_model is not None else base.served_model,
        api_key=merged_api_key,
    )


async def probe_endpoint(target: ProbeTarget) -> EndpointProbeResult:
    """向该端点发一次最小 chat 请求并回报结论。

    除入参问题（协议无法推导探测地址）外**从不抛异常**：探测失败是「此刻不通」这个业务
    结论，不是本函数的异常——调用方（闸门 / 诊断接口）各自决定怎么表达它。

    双层时限：``httpx`` 的分段超时（同一常量）管「连接不上/两次读之间卡死」的快速失败，
    ``asyncio.timeout`` 管**整次探测的墙钟上限**。只有后者能真正兑现「单次探测不超过
    ``settings.endpoint_probe_timeout``」——分段超时管的是「两次读之间」，滴答式上游可以
    逐段续命，把一次人工操作拖成分钟级。

    地址无法解析（端口非数字、URL 括坏等）抛 422 而非 500：这是入参问题，与协议未知
    同类。解析发生在两处、抛两种异常，分别拦——URL 规则层 urlsplit 抛裸 ValueError
    （构造期），httpx.InvalidURL 也不是 HTTPError 子类（发请求时）。
    """
    try:
        url = _probe_url(target)
        body, headers = _probe_request(target)
    except ValueError as e:
        raise LLMPodValidationError(f"端点地址无法解析（api_base={target.api_base!r}）：{e}") from e
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.endpoint_probe_timeout):
            async with httpx.AsyncClient(timeout=settings.endpoint_probe_timeout) as client:
                # 流式收体并设字节上限：非流式 post 会把整个响应体物化进内存，错配地址
                # （文件服务/模型 blob/解压炸弹）在墙钟内可轻松灌满管理端内存；上限取
                # 判据所需的解析余量，超了就当「不像最小 chat 应答」处理
                chunks: list[bytes] = []
                received = 0
                overflow = False
                async with client.stream("POST", url, content=json_payload(body), headers=headers) as resp:
                    async for chunk in resp.aiter_bytes():
                        received += len(chunk)
                        if received > _PROBE_BODY_BYTES:
                            overflow = True
                            break
                        chunks.append(chunk)
                    body_bytes = b"".join(chunks)
                    status_code = resp.status_code
    except httpx.InvalidURL as e:
        raise LLMPodValidationError(f"端点地址无法解析（api_base={target.api_base!r}）：{e}") from e
    except httpx.TimeoutException as e:
        # httpx 的分段超时（连接/读/写）与墙钟超时同一口径归「探测超时」：归连接失败
        # 会让 422 标题说连接失败、细节说超时，自相矛盾
        return EndpointProbeResult(
            healthy=False,
            url=url,
            detail=f"探测超时（超过 {settings.endpoint_probe_timeout:g}s，{type(e).__name__}）",
            elapsed_ms=_ms(started),
        )
    except TimeoutError:
        return EndpointProbeResult(
            healthy=False,
            url=url,
            detail=f"探测超时（超过 {settings.endpoint_probe_timeout:g}s）",
            elapsed_ms=_ms(started),
        )
    except httpx.HTTPError as e:
        return EndpointProbeResult(healthy=False, url=url, detail=_exc_detail(e), elapsed_ms=_ms(started))

    elapsed_ms = _ms(started)
    text = body_bytes[:_DETAIL_BYTES].decode("utf-8", "replace")
    if 200 <= status_code < 300 and not overflow:
        # 2xx 还须是 JSON：网关真实转发会对响应 resp.json()（openai/anthropic 适配器），
        # 打错端口/网关默认页/SPA 兜底页回的是 200+HTML——只看状态码会写成假健康，真流量
        # 一来就 502。判据与网关对齐：状态码 + 可解析的 JSON 体。
        try:
            json.loads(body_bytes)
        except ValueError:
            return EndpointProbeResult(
                healthy=False, url=url, status_code=status_code, detail=_clamp(text), elapsed_ms=elapsed_ms
            )
        return EndpointProbeResult(healthy=True, url=url, status_code=status_code, elapsed_ms=elapsed_ms)
    if overflow:
        # 超字节上限的响应不是最小 chat 应答该有的形状（错配地址指向文件服务/大 blob）；
        # 流式收体即断，整包从未进内存。status_code 置 None：这是**我方的体量判据**
        # 不是上游的拒绝应答，/test 的写回条件（上游真的应答过）不该把它当 down 依据
        return EndpointProbeResult(
            healthy=False,
            url=url,
            detail=f"响应体超过 {_PROBE_BODY_BYTES} 字节，不是最小 chat 应答的形状",
            elapsed_ms=elapsed_ms,
        )
    return EndpointProbeResult(
        healthy=False, url=url, status_code=status_code, detail=_clamp(text), elapsed_ms=elapsed_ms
    )


async def ensure_reachable(
    target: ProbeTarget,
    *,
    stored: ProbeTarget | None = None,
    skip_probe: bool = False,
) -> bool:
    """接入/编辑前置闸门：探测不通即 422，调用方据此**不落库**（行未被触碰）。

    返回**本次是否真的探过并通过**（False = 被下面三档跳过；不通则抛异常，不会返回
    False）——即「这份配置此刻被上游验证过」。调用方据此把该行健康拨回 healthy：配置被
    探通就说明它此刻真能用，不必等下一次调用去自愈。

    三档跳过集中在此一处，路由层不必各自记得：

    - 全局开关 ``settings.endpoint_probe_enabled``：容器无外网出口时所有外部端点都登记
      不进来，留一个整体兜底；
    - 请求体 ``skip_probe``：逐条放行；
    - ``stored`` 与 ``target`` 相等：连接配置没变。编辑弹窗每次提交都回传 api_base /
      path / name，按「字段有没有传」判定会让每次改名都插一次上游往返，上游抖动时连
      改名都被 422 拦住；而「启用/停用」开关也走 update 接口，那样连停用一个端点都得
      上游当场通得过。故按**值**比较，而非按字段出现与否。
    """
    if not settings.endpoint_probe_enabled or skip_probe:
        return False
    if target.matches(stored):
        return False
    result = await probe_endpoint(target)
    if not result.healthy:
        raise LLMPodValidationError(_reject_message(result))
    return True


def _probe_url(target: ProbeTarget) -> str:
    """探测地址：与真实转发同一份 URL 规则（登记了主路径走主路径，否则接协议缺省主路径）。

    缺省主路径表与网关适配器同值，由 tests/unit/test_endpoint_probe.py 的一致性守卫
    对着适配器本体锁住——探的地址一旦与真实流量分叉，「探 A 的结论写去 B 的健康」就回来了。

    协议未知（含空串，如 ``/test`` 既不给 ``endpoint_id`` 又不给 ``protocol``）时**抛错
    而非回落 openai**：静默按 openai 探一个 anthropic 端点，探的是错地址，结论要么是假
    健康、要么是假 422——正是本能力要消灭的那类信号。这是本模块唯一的「入参问题」抛错。
    """
    default_path = _CHAT_PATH.get(target.protocol)
    if default_path is None:
        raise LLMPodValidationError(
            f"无法推导探测地址：协议 {target.protocol!r} 未知（应为 {' / '.join(sorted(_CHAT_PATH))}）"
        )
    return resolve_url(_route_target(target), default_path)


def _route_target(target: ProbeTarget) -> RouteTarget:
    """探测用的 RouteTarget：只为复用网关 ``resolve_url`` 的 URL 规则（只此一个查询出口）。

    weight / priority / engine_type / endpoint_id 与 URL 推导无关，填中性值；探测不产用量、
    不落 request_log。刻意**不改** ``resolve_url`` 的签名（收窄成 ``(api_base, path, ...)``
    确实更贴切，但那是网关的公共 API、别人在用），代价就是这里多一个只填了 URL 相关字段的
    对象——比在管理端自己拼一遍 URL、与真实转发各演一套要安全。
    """
    return RouteTarget(
        endpoint_id=0,
        deployment_id=None,
        api_base=target.api_base,
        path=target.path,
        served_model=target.served_model,
        engine_type="external",
        weight=0,
        priority=0,
        protocol=target.protocol,
        api_key=target.api_key,
    )


def _probe_request(target: ProbeTarget) -> tuple[dict, dict]:
    """最小 chat 请求体与鉴权头（对齐网关适配器的头规则）。

    用真调用而非 ``GET /models``：models 列表能验出 Key 与地址，验不出模型名与主路径；
    而一次最小真实调用就把 Key / 地址 / 模型名 / 路径四件事一起验了。

    ``max_tokens`` 只在 anthropic 协议带（其 API 必填）：openai 系的推理模型
    （o 系/gpt-5 类）会把 ``max_tokens`` 直接 400 拒掉——网关转发客户端原载荷
    （用的是 ``max_completion_tokens``）没问题，探测却会假 422。代价是 openai 协议
    的探测不再封顶输出长度（墙钟 ``endpoint_probe_timeout`` 兜底，外部端点单次
    最小提问的输出开销可忽略）。

    openai 系鉴权头复用网关的 ``bearer_headers``；anthropic 的头在适配器里是私有实现，
    三行规则在此自持（一致性守卫直接比对适配器实际发出的请求，抄错会红）。
    """
    body = {"model": target.served_model, "messages": [{"role": "user", "content": _PROBE_PROMPT}]}
    if target.protocol == "anthropic":
        body["max_tokens"] = 1  # anthropic 必填；给最小值省一次完整生成
        headers = {"content-type": "application/json", "anthropic-version": _ANTHROPIC_VERSION}
        if target.api_key:
            headers["x-api-key"] = target.api_key
        return body, headers
    return body, bearer_headers(target.api_key)


def _reject_message(result: EndpointProbeResult) -> str:
    """422 文案：探的地址 + 上游原话（不翻译，运维要看到上游怎么说的）。

    超时单列：它混不进「HTTP 状态码」也混不进「连接失败」（detail 写着探测超时），
    归到连接失败会让标题与细节自相矛盾，把运维的注意力引去查 DNS/网络。
    """
    if result.status_code:
        where = f"{result.url} → HTTP {result.status_code}"
    elif result.detail and (result.detail.startswith("探测超时") or result.detail.startswith("响应体超过")):
        # 「响应体超上限」也是上游已应答的结论（地址指到了文件服务/大 blob 一类），
        # 归连接失败同样自相矛盾
        where = f"{result.url} → 响应异常"
    else:
        where = f"{result.url} → 连接失败"
    return f"端点探测未通过（{where}）：{result.detail or '（上游未返回内容）'}"


def _exc_detail(e: httpx.HTTPError) -> str:
    """连接类失败的原文，带异常类名。

    类名不是分类层：``ConnectTimeout`` 与 ``ConnectError`` 的 ``str`` 都可能为空，只靠
    message 会得到一片空白，运维分不出「DNS 解析不了」与「TCP 连不上」。
    """
    text = str(e)
    return _clamp(f"{type(e).__name__}: {text}" if text else type(e).__name__)


def _clamp(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _DETAIL_LIMIT else text[:_DETAIL_LIMIT] + "…"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
