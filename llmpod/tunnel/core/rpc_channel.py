"""RPC 通道 (JSON-RPC 2.0 + 流式扩展) — 隧道系统的核心。

Master 和 worker 各持有一个 RpcChannel 实例,共享此实现:
- Master: 使用 call_unary/stream 发起调用,接收 result/stream;处理 $hello/$heartbeat 通知。
- Worker: 接收调用并通过反射分发;发送 $hello/$heartbeat 通知。

单个 WebSocket,通过 id 多路复用: 一元调用挂起 Future,流式挂起 StreamContext。
每个帧生成独立任务,因此单个连接上的多个调用/流互不阻塞。
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Awaitable, Callable

from ..entity import protocol as P
from ..entity.errors import RemoteError
from .registry import ServiceRegistry

log = logging.getLogger("llmpod.tunnel.channel")


def _json_default(o):
    """json.dumps 兜底: Pydantic model → dict(model_dump); 其他 → str()。

    仅在 json.dumps 遇到非原生 JSON 类型时触发(纯 dict 帧零开销)。
    """
    dump = getattr(o, "model_dump", None)
    if callable(dump):
        return dump()
    return str(o)


class StreamContext:
    """流式调用的本地消费者端(异步迭代器)。

    Master 端调用流将其挂入 _pending[id]; worker 推送每个流帧
    到队列; master 的 `async for item in ctx` 从中拉取。
    有界队列(默认 128): 当消费者较慢时,推送阻塞 → 对 worker 反压,
    防止慢消费者导致内存无限增长。
    结束/错误使用标志 + 哨兵唤醒,永不阻塞,队列满时不丢失。
    """

    _SENTINEL = object()

    def __init__(self, req_id: str, maxsize: int = 128):
        self.req_id = req_id
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._term: tuple | None = None  # ("end",) | ("err", exc)

    async def push(self, value):
        """推送一帧。队列满时阻塞 → 对 worker 反压。已终止时丢弃。"""
        if self._term is None:
            await self._q.put(value)

    def abandon(self):
        """消费者放弃后放行被反压阻塞的推送方（读循环）。

        数据帧在读取循环内直接 await 入队（真背压）：消费者断开时若读循环
        正卡在满队列的 put() 上，无人再 get()，put 永不返回 → 读循环永久
        悬挂、_on_closed 永不触发（通道假死：入站全停、出站照发）。
        asyncio.Queue 腾空位**不会**唤醒已挂起的 putter（唤醒只发生在
        get()/task_done() 路径）——须取消挂起的 putter future：被取消的
        put 抛 CancelledError 回到读循环，由 ``_stream_push`` 按「消费者已
        放弃，丢帧继续」吞掉（见其注释），连接本身不受牵连。此后 _pending
        已无此 rid，同流后续帧经 _stream_push 直接丢弃不再阻塞。
        """
        if self._term is None:
            self._term = ("end", None)
        for putter in list(self._q._putters):
            if not putter.done():
                putter.cancel()
        self._wake()

    def end(self):
        """标记流正常结束(收到 stream_end 帧)。"""
        if self._term is None:
            self._term = ("end", None)
            self._wake()

    def error(self, exc: Exception):
        """标记流错误结束(收到错误帧或连接断开)。"""
        if self._term is None:
            self._term = ("err", exc)
            self._wake()

    def _wake(self):
        """放入哨兵唤醒可能阻塞的消费者。队列满时跳过(消费者会看到 _term)。"""
        try:
            self._q.put_nowait(self._SENTINEL)
        except asyncio.QueueFull:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._term is not None and self._q.empty():
            self._raise_term()
        item = await self._q.get()
        if item is self._SENTINEL:
            if self._term is not None:
                self._raise_term()
            return await self.__anext__()
        return item

    def _raise_term(self):
        """根据终止标志抛出异常: end→StopAsyncIteration, err→抛出异常。"""
        kind, val = self._term
        if kind == "end":
            raise StopAsyncIteration
        raise val


class RpcChannel:
    """基于单个 WebSocket 连接的 JSON-RPC 多路复用通道。

    Master/worker 共享此实现:
    - Master 传入 registry=None (仅发送调用,接收结果)
    - Worker 传入 registry (接收调用,通过反射分发)
    使用 _pending[id] 进行请求-响应匹配, _tasks[id] 跟踪进行中的调用(用于 $cancel)。
    """

    def __init__(
        self,
        transport,
        registry: ServiceRegistry | None = None,
        name: str = "channel",
        on_hello: Callable[[dict], Awaitable[None]] | None = None,
        on_heartbeat: Callable[[dict], Awaitable[None]] | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
        on_register_ack: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.transport = transport
        self.registry = registry  # Worker 需要此注册表; master 传入 None
        self.name = name
        self.on_hello = on_hello
        self.on_heartbeat = on_heartbeat
        self.on_close = on_close
        self.on_register_ack = on_register_ack
        # 扩展通知 handler：method → coroutine（$ 前缀协议帧之外的 server→node 通知，
        # 如 $node_upgrade / $node_decommission）。on_* 特例字段保留不动（既有绑定路径）。
        self.notification_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._pending: dict = {}  # id -> Future | StreamContext (master 端等待结果)
        self._tasks: dict = {}  # id -> asyncio.Task (worker 端进行中的调用,用于 $cancel)
        self._send_lock = asyncio.Lock()  # 序列化单连接上的出站帧
        self._hello_lock = asyncio.Lock()  # 同连接多个 hello 不得并发绑定不同节点
        self._closed = asyncio.Event()
        self._bg_tasks: set = set()
        self.node_id: str | None = None  # 由 master 在 on_hello 中设置

    def on_notification(self, method: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        """注册扩展通知 handler（幂等：后注册覆盖先注册）。"""
        self.notification_handlers[method] = handler

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def start(self):
        """启动读取循环(后台任务,每帧生成独立的处理任务)。"""
        asyncio.create_task(self._read_loop())

    async def wait_closed(self):
        """阻塞直至通道关闭。供 accept_worker 调用方使用的公开 API。"""
        await self._closed.wait()

    async def send_notification(self, method: str, params: dict):
        """发送通知帧($hello/$heartbeat 等,无 id,不期望响应)。公开 API。"""
        await self._send(P.notification(method, params))

    async def _read_loop(self):
        """持续读取帧 → json.loads → 逐帧处理。

        流帧的入队反压必须作用在读取循环上，而不是派生任务：每帧起独立任务、
        队列满时阻塞的是任务本身，慢消费者下读取循环照常收帧起新任务，
        _bg_tasks 无界增长（内存撑爆）且反压名存实亡。因此流帧（stream:true）
        在读取循环内直接 await 入队；非流帧（请求/通知/响应，处理轻）仍走
        并发任务，避免单帧处理慢拖慢整条连接的读节奏。
        """
        try:
            while True:
                try:
                    raw = await self.transport.receive_text()
                except Exception:
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("stream") is True and "method" not in msg:
                    # worker → master 的流数据帧：入队阻塞即反压，读到 TCP 背压
                    await self._stream_push(msg.get("id"), msg.get("result"))
                    continue
                task = asyncio.create_task(self._handle(msg))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        finally:
            await self._on_closed()

    async def _on_closed(self):
        """连接关闭: 拒绝所有待处理请求,取消所有任务,触发 on_close 回调。幂等。"""
        if self._closed.is_set():
            return
        self._closed.set()
        for rid, pending in list(self._pending.items()):
            if isinstance(pending, asyncio.Future):
                if not pending.done():
                    pending.set_exception(RemoteError("channel closed", code=P.CHANNEL_CLOSED))
            else:
                pending.error(RemoteError("channel closed", code=P.CHANNEL_CLOSED))
        self._pending.clear()
        for rid, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        for task in list(self._bg_tasks):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        if self.on_close:
            try:
                await self.on_close()
            except Exception:
                log.exception("on_close callback error")

    async def close(self):
        """主动关闭: 关闭传输层 + 触发 _on_closed 清理。"""
        try:
            await self.transport.close()
        except Exception:
            pass
        await self._on_closed()

    async def _send(self, frame: dict):
        """JSON 编码 + 加锁发送(序列化以避免帧交错)。

        default 回调仅在遇到非原生 JSON 类型时触发(纯 dict/list/scalar 帧零开销):
        Pydantic model → model_dump(); 其他 → str()。
        """
        data = json.dumps(frame, ensure_ascii=False, default=_json_default)
        async with self._send_lock:
            await self.transport.send_text(data)

    async def _handle(self, msg: dict):
        """按 JSON-RPC 2.0 字段语义分发帧。

        有 method: 请求(有 id) → _handle_call; 通知(无 id) → $cancel/$hello/$heartbeat/$register_ack。
        无 method: 有 error → _reject; stream:true → _stream_push;
        有 result → _resolve (一元) 或 _stream_end (流结束),通过 _pending 类型区分。
        """
        if self.closed:
            return
        if "method" in msg:
            method = msg["method"]
            if "id" in msg:
                await self._handle_call(msg)
            else:
                params = msg.get("params") or {}
                if method == "$cancel":
                    self._cancel_local(params.get("id"))
                elif method == "$hello" and self.on_hello:
                    async with self._hello_lock:
                        await self.on_hello(params)
                elif method == "$heartbeat" and self.on_heartbeat:
                    await self.on_heartbeat(params)
                elif method == "$register_ack" and self.on_register_ack:
                    await self.on_register_ack(params)
                elif method in self.notification_handlers:
                    handler = self.notification_handlers[method]
                    try:
                        await handler(params)
                    except Exception:
                        log.exception("notification handler %s error", method)
            return
        rid = msg.get("id")
        try:
            if "error" in msg:
                err = msg["error"]
                self._reject(
                    rid, err.get("code", P.SERVER_ERROR), err.get("message", "remote error"), err.get("data", "")
                )
            elif msg.get("stream") is True:
                await self._stream_push(rid, msg.get("result"))
            elif "result" in msg:
                pending = self._pending.get(rid)
                if isinstance(pending, asyncio.Future):
                    self._resolve(rid, msg.get("result"))
                elif isinstance(pending, StreamContext):
                    self._stream_end(rid)
        except Exception:
            log.exception("handle msg=%s", msg)

    # ---- Worker 端: 处理入站调用 ----
    async def _handle_call(self, msg: dict):
        """Worker 接收调用: 拆分 method→service/method,通过反射分发,返回成功或生成 _run_stream。"""
        rid = msg["id"]
        full = msg["method"]
        service, _, method = full.partition(".")
        params = msg.get("params") or {}
        self._tasks[rid] = asyncio.current_task()
        try:
            kind, result = await self.registry.dispatch(service, method, params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._send(P.error(rid, P.SERVER_ERROR, repr(e), traceback.format_exc()))
            return
        finally:
            self._tasks.pop(rid, None)
        if kind == "stream":
            task = asyncio.create_task(self._run_stream(rid, result))
            self._tasks[rid] = task
        else:
            await self._send(P.success(rid, result))

    async def _run_stream(self, rid: str, agen):
        """Worker 流推送: 迭代异步生成器,每项发送 stream_frame,结束时发送 stream_end。取消 → aclose。"""
        self._tasks[rid] = asyncio.current_task()
        try:
            async for item in agen:
                if self._closed.is_set():
                    break
                await self._send(P.stream_frame(rid, item))
            await self._send(P.stream_end(rid))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._send(P.error(rid, P.SERVER_ERROR, repr(e), traceback.format_exc()))
        finally:
            # 所有退出路径（正常结束 / closed-break / 取消 / 异常）都关闭底层生成器，
            # 避免泄漏 docker pull 等子进程（aclose 幂等，已结束的生成器再调为 no-op）
            try:
                await agen.aclose()
            except Exception:
                pass
            self._tasks.pop(rid, None)

    def _cancel_local(self, rid):
        """Worker 接收 $cancel: 取消进行中的调用/流任务(尽力而为)。"""
        task = self._tasks.pop(rid, None)
        if task is not None and not task.done():
            task.cancel()

    # ---- Master 端: 发起调用 ----
    async def call_unary(self, service: str, method: str, params: dict, timeout: float | None = None):
        """Master 一元调用: 发送请求,挂起 Future,等待结果。超时/取消时发送 $cancel。"""
        rid = P.new_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send(P.request(f"{service}.{method}", params, rid))
        except BaseException:
            # send 抛错/发送中被取消：挂起项无人再解析，立即摘除防泄漏
            # （断连清理之外的唯一出口）
            self._pending.pop(rid, None)
            raise
        try:
            if timeout:
                return await asyncio.wait_for(fut, timeout)
            return await fut
        except asyncio.TimeoutError:
            await self._cancel(rid)
            raise RemoteError(f"RPC 调用超时（{timeout} 秒）", code=P.TIMEOUT)
        except asyncio.CancelledError:
            await self._cancel(rid)
            raise
        finally:
            self._pending.pop(rid, None)

    async def stream(self, service: str, method: str, params: dict):
        """Master 流式调用: 发送请求,挂起 StreamContext,异步迭代流帧。消费者断开时发送 $cancel。"""
        rid = P.new_id()
        ctx = StreamContext(rid)
        self._pending[rid] = ctx
        try:
            await self._send(P.request(f"{service}.{method}", params, rid))
        except BaseException:
            self._pending.pop(rid, None)
            raise
        try:
            async for item in ctx:
                yield item
        finally:
            self._pending.pop(rid, None)
            # 先放行再取消：数据帧在读取循环内直接 await 入队（真背压），若此刻
            # 读循环卡在满队列的 put() 上，$cancel 的响应帧也读不进来——abandon
            # 取消卡住的 putter，读循环才走得到 _cancel 发送与后续清理
            ctx.abandon()
            await self._cancel(rid)

    # ---- 内部处理器 ----
    def _resolve(self, rid, value):
        """收到一元结果: 按 id 查找 Future,set_result 唤醒等待者。"""
        fut = self._pending.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_result(value)

    def _reject(self, rid, code, message, data=""):
        """收到错误: 按 id 查找 Future/StreamContext,set_exception/error 注入异常。"""
        pending = self._pending.pop(rid, None)
        if pending is None:
            return
        err = RemoteError(message, tb=str(data), code=code)
        if isinstance(pending, asyncio.Future):
            if not pending.done():
                pending.set_exception(err)
        else:
            pending.error(err)

    async def _stream_push(self, rid, value):
        """收到流帧(stream:true): 按 id 查找 StreamContext,推入队列(满时阻塞 → 反压)。

        消费者放弃（``stream()`` finally 的 abandon）会取消满队列上挂起的
        putter，put 以 CancelledError 返回——此处按「消费者已放弃，丢帧继续」
        吞掉，读循环继续处理后续帧；连接与同连接上的其他调用不受牵连。
        读循环任务本身不会被外部 cancel（关闭走 transport.close() → receive
        抛错路径），能到达这里的取消只有 abandon 一种来源。
        """
        pending = self._pending.get(rid)
        if isinstance(pending, StreamContext):
            try:
                await pending.push(value)
            except asyncio.CancelledError:
                if pending._term is not None:
                    return
                raise

    def _stream_end(self, rid):
        """收到流结束帧(无 stream 的 result): 按 id 查找 StreamContext,结束迭代。"""
        pending = self._pending.pop(rid, None)
        if isinstance(pending, StreamContext):
            pending.end()

    async def _cancel(self, rid):
        """Master → worker: 发送 $cancel 通知以取消调用(尽力而为,连接已关闭时静默)。"""
        try:
            await self._send(P.notification("$cancel", {"id": rid}))
        except Exception:
            pass
