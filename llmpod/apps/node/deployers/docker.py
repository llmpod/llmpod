"""DockerDeployer：基于 docker SDK 的部署执行器。

把 ``DeploySpec`` 翻译成 docker SDK 调用（``client.containers.create/start/stop/remove/...``）。
按 spec 做 spec 字段→SDK kwargs 映射（非全直传）。逻辑从原 ``apps/node/ops/docker.py``
整体搬入，零行为改动；``_get_client`` 保留模块级单例。

Node 侧不做任何 arch/引擎分支——所有差异由 Manager 适配器 ``build_device_config`` 在 spec
里组装好。本类只认字段→SDK 映射。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, NamedTuple

import docker
import requests.exceptions
from docker.auth import resolve_repository_name
from docker.errors import APIError, ImageNotFound, NotFound, StreamParseError
from docker.types import DeviceRequest

from llmpod.apps.node.deployers.base import Deployer
from llmpod.apps.node.deployers.image_size import probe_image_layers
from llmpod.apps.node.exceptions import ContainerGone, TaskCancelled, TaskExecutionError
from llmpod.config.settings import settings
from llmpod.tunnel.schemas import (
    ContainerState,
    ContainerSummary,
    DeployResult,
    DeploySpec,
    RemoveResult,
    StopResult,
)

log = logging.getLogger("llmpod.apps.node.deployers.docker")

_client: docker.DockerClient | None = None


def _get_client() -> docker.DockerClient:
    """惰性获取 docker SDK 客户端（进程内单例）。"""
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def get_docker_client() -> docker.DockerClient:
    """docker 客户端唯一入口（node 侧各服务共用）。

    生产拓扑只把 docker.sock 挂进容器、容器内没有 docker CLI——所有 docker 操作
    必须走 SDK 经 sock，不允许起 ``docker`` 命令子进程。
    """
    return _get_client()


# ---- pull_image 流式消费（SDK stream chunk 的线程跳与 layer 聚合）----

# 迭代器耗尽哨兵：线程内 StopIteration 转普通返回值，避免跨线程/协程抛 StopIteration。
# 拉取流（DockerDeployer.pull_image、RPC docker_pull）与 tail_log 的日志流消费共用。
STREAM_DONE = object()


def open_pull_stream(image: str):
    """打开低层 ``api.pull`` 的流式 chunk 迭代器（docker 客户端归此处统一下发）。

    必须走低层 ``api.pull``：高层 ``images.pull()`` 会吞掉 stream 参数、同步消费
    完整拉取后返回 ``Image`` 对象（迭代即 ``TypeError('Image' object is not
    iterable)``），流式进度拿不到。部署路径（DockerDeployer.pull_image）与
    RPC 侧（services/node.docker_pull）共用本入口，「pull 只能走低层」只此一份。
    """
    client = _get_client()
    return iter(client.api.pull(image, stream=True, decode=True))


def next_stream_chunk(iterator):
    """阻塞迭代器取下一块；耗尽返回 STREAM_DONE 哨兵（跨线程消费的唯一取数口）。"""
    try:
        return next(iterator)
    except StopIteration:
        return STREAM_DONE


# 完成态 chunk 的 status（daemon 对本地已缓存/已完成的层不再发 progressDetail）：
# 镜像预探测的 digest→size 清单按这些状态把层直接记满（分子到不了头的问题见
# _update_pull_layers docstring）
_COMPLETE_STATUSES = ("Already exists", "Download complete", "Pull complete")


def _update_pull_layers(
    layers: dict[str, tuple[int, int]], chunk, known_blobs: dict[str, int] | None = None
) -> tuple[int, int]:
    """单个进度 chunk → 更新 layer 表，返回 (bytes_done, bytes_total) 全层累计。

    带 ``progressDetail`` 的 chunk 按 ``id`` 索引到对应 layer，同层取 max（防同层
    回退）；status/aux 等非进度 chunk 只参与累计。分母只计已出现层。

    ``known_blobs``（预探测的 digest→size 清单）补完成态：本地已缓存的层 daemon 只发
    ``Already exists``、下完/解压完只发 ``Download complete``/``Pull complete``——没有
    progressDetail 的 chunk 原本进不了分子，会把「种入预探测总量」的百分比永久压低
    （基础层大量共享的 CUDA 系镜像最严重）。按 stream id 与 digest 的前缀关系配对出
    该层大小，直接记满。
    """
    if not isinstance(chunk, dict):
        return _sum_layers(layers)
    detail = chunk.get("progressDetail") or {}
    cur, tot = detail.get("current"), detail.get("total")
    lid = chunk.get("id")
    if lid and isinstance(cur, int) and isinstance(tot, int) and tot > 0:
        prev_cur, _ = layers.get(lid, (0, 0))
        layers[lid] = (max(prev_cur, cur), tot)
    elif known_blobs and lid and chunk.get("status") in _COMPLETE_STATUSES:
        size = _known_blob_size(known_blobs, lid)
        if size:
            prev_cur, _ = layers.get(lid, (0, 0))
            layers[lid] = (max(prev_cur, size), size)
    return _sum_layers(layers)


def _sum_layers(layers: dict[str, tuple[int, int]]) -> tuple[int, int]:
    """layer 表 → (累计已下, 累计分母)。"""
    done = sum(cur for cur, _ in layers.values())
    total = sum(t for _, t in layers.values())
    return done, total


def _known_blob_size(known_blobs: dict[str, int], lid: str) -> int | None:
    """stream id（digest 的短前缀，12+ hex）→ 该 blob 大小；配不上返回 None。

    探测清单入库时（``image_download_size``）已把每个 digest 的 12 位前缀存为别名，
    这里通常一次 dict 命中；更长/更短的 id 走 startswith 兜底。
    """
    size = known_blobs.get(lid)
    if size:
        return size
    for digest, sz in known_blobs.items():
        if digest.startswith(lid):
            return sz
    return None


def pull_stream_error(chunk) -> str | None:
    """daemon 拉取中途失败（解压层失败/磁盘写满/registry 中断）时 HTTP 仍 200，错误以
    ``{"error": ...}`` chunk 随流下发、流随即正常结束——低层 ``api.pull`` 不抛。

    统一识别口（``iter_pull_chunks`` 消费时调用）；返回错误文案或 None。
    """
    if isinstance(chunk, dict) and chunk.get("error"):
        return str(chunk["error"])
    return None


def fetch_image(image: str):
    """``images.get`` 的同步线程体：返回 Image，docker 异常原样上抛。

    存在性探测的唯一底层（``image_present_sync`` / ``verify_image_present`` 共用），
    「不存在」（ImageNotFound）与「daemon 错误」由各调用方按自己的语义映射。
    """
    return _get_client().images.get(image)


def image_present_sync(image: str) -> bool:
    """存在性判定线程体：ImageNotFound → False；daemon 错误原样上抛。

    「确认不在位」与「探测通道不可用」必须可区分——manager 侧 SUCCESS 行复核靠
    异常区分两者：daemon 抖动若被塌缩成 False，在位镜像会被误判缺失、触发无谓
    重拉（连续数轮可耗尽重试上限）。吞 daemon 错误的调用方（如 Deployer.
    ``image_exists`` 供 executor 短路用）自行包一层映射。
    """
    try:
        fetch_image(image)
        return True
    except ImageNotFound:
        return False


def _image_exists_swallowing(image: str) -> bool:
    """executor 短路口径：daemon 错误（APIError）也不当作存在——返回 False 并告警，
    留给 pull 流程暴露真因。其余异常（连接层）照原样上抛，由 executor 的
    「探测异常不阻断」兜住。"""
    try:
        return image_present_sync(image)
    except APIError as e:
        log.warning("image_exists %s: docker error: %s", image, e.explanation)
        return False


async def verify_image_present(image: str) -> None:
    """流结束后复核镜像确已落盘；缺失/探测异常均抛 TaskExecutionError。

    「拉过了就必须在」的硬断言：流结束 ≠ 落盘（可能被静默截断）。daemon 瞬时
    不可用（重启窗口）也如实上报交上层重试，下一轮 attempt 的 image_exists
    短路会自愈，不静默放过。
    """
    try:
        await asyncio.to_thread(fetch_image, image)
    except ImageNotFound as e:
        raise TaskExecutionError(
            f"镜像拉取失败（image={image}）: 流结束但镜像未落盘（连接中断或 daemon 中止），请重试"
        ) from e
    except Exception as e:
        raise TaskExecutionError(f"镜像拉取后校验失败（image={image}）: {e}") from e


async def iter_pull_chunks(image: str, cancel_event: asyncio.Event | None = None) -> AsyncIterator[dict]:
    """低层拉取流的唯一消费口（部署路径 ``DockerDeployer.pull_image`` 与 RPC
    ``docker_pull`` 共用）——拉取流的正确性契约在此一次成立，新消费者不得绕过
    本口自行直连 ``open_pull_stream``：

    - daemon 中途失败以 ``{"error": ...}`` chunk 随流下发、流随即正常结束（低层
      ``api.pull`` 不抛）→ 转 TaskExecutionError；漏掉会把失败拉取当成功上报
      （镜像从未落盘，下游 docker run 才爆 No such image）；
    - 流静默截断（无 error chunk、流正常结束）→ 流末 :func:`verify_image_present`
      复核落盘，缺失按失败收尾；
    - 异常/取消路径确定性 close 迭代器（未读的 chunked 响应不留给 GC 兜底）。
      cancel 检查只在 chunk 间隙进行、next() 从不在途，close 不会撞
      「generator already executing」。

    产出原始 dict chunk（含 status/progressDetail 等）；进度聚合 / 文本化由
    调用方各自做。打开/连接层失败（401、404、截断在 JSON 行中间的
    StreamParseError、requests 系连接错误）统一包装为 TaskExecutionError。
    """
    iterator: Iterator | None = None
    try:
        iterator = await asyncio.to_thread(open_pull_stream, image)
        while True:
            chunk = await asyncio.to_thread(next_stream_chunk, iterator)
            if chunk is STREAM_DONE:
                break
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled(f"镜像拉取已取消: {image}")
            err = pull_stream_error(chunk)
            if err is not None:
                raise TaskExecutionError(f"镜像拉取失败（image={image}）: {err}")
            yield chunk
    except TaskCancelled:
        raise
    except (APIError, ImageNotFound, NotFound, StreamParseError, requests.exceptions.RequestException) as e:
        raise TaskExecutionError(f"镜像拉取失败（image={image}）: {e}") from e
    finally:
        if iterator is not None:
            with suppress(Exception):
                await asyncio.to_thread(iterator.close)
    await verify_image_present(image)


# per-name deploy 锁：序列化同名容器的 create/replace，防并发 409 兜底破坏性重建。
# 锁下沉到 Deployer 层（模块级、跨实例共享），ContainerServiceImpl（RPC 路径）与
# 执行器（直调 deploy）都受同一把锁保护。
@dataclass
class _NameLockEntry:
    lock: asyncio.Lock
    users: int  # 取锁未还的协程数；归零即从表里回收，防容器名缓慢泄漏


_deploy_locks: dict[str, _NameLockEntry] = {}
_deploy_locks_guard = asyncio.Lock()


@asynccontextmanager
async def _deploy_lock(name: str):
    """按容器名串行化的临界区；无名容器不加锁。

    回收以「在用计数」判定而非 lock.locked()：release 到 waiter 真正接管之间
    locked() 会瞬时为 False，按它删除会让后来者另开新锁、破掉互斥。
    """
    if not name:
        yield
        return
    async with _deploy_locks_guard:
        entry = _deploy_locks.get(name)
        if entry is None:
            entry = _NameLockEntry(lock=asyncio.Lock(), users=0)
            _deploy_locks[name] = entry
        entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:
            async with _deploy_locks_guard:
                if _deploy_locks.get(name) is entry and entry.users == 0:
                    del _deploy_locks[name]


class ProbeEntry(NamedTuple):
    """单镜像预探测结果。

    ``aliases`` 供流 id 配对：full digest 与 12 位前缀两个键指向同一 blob，**不可对
    其求和**（会双计）；总量用探测时记下的 ``unique_total``。
    """

    aliases: dict[str, int]
    unique_total: int


class DockerDeployer(Deployer):
    """docker SDK 后端 Deployer。"""

    # 预探测清单（ref → ProbeEntry），有界。探测与拉取**并行**，清单无法经参数
    # 传递，只能实例内衔接（executor 共用同一 deployer 实例）；丢了就回落按已出现
    # 层聚合，无正确性影响
    _PROBE_LAYERS_MAX = 16

    def __init__(self) -> None:
        self._probe_layers: dict[str, ProbeEntry] = {}

    @staticmethod
    def _parse_gpus(gpus: list[int] | None) -> list[DeviceRequest] | None:
        """解析 spec 的 gpus 字段（``list[int] | None``）为 docker SDK device_requests。

        - None → None（不分配 GPU，不带 device_requests）
        - [] 空表 → [DeviceRequest(count=-1)]（全部 GPU）
        - [0, 1] → [DeviceRequest(device_ids=["0", "1"])]
        """
        if gpus is None:
            return None
        if len(gpus) == 0:
            return [DeviceRequest(count=-1, capabilities=[["compute"]])]
        return [DeviceRequest(device_ids=[str(i) for i in gpus], capabilities=[["compute"]])]

    @staticmethod
    def _rewrite_model_host_path(host: str, store_dir: str, host_store_dir: str) -> str:
        """模型卷 host 侧前缀改写：canonical 容器路径 → 宿主真实路径。

        spec 的模型卷 host 键是 canonical 容器内路径（manager 下发的节点视角，与
        节点容器内 ``model_store_dir`` 一致）；推理容器是经 docker.sock 创建的兄弟
        容器，host 侧按宿主字面路径解析。宿主大盘挂在别处时（引导参数
        ``model_host_dir``，经 env ``LLMPOD_MODEL_HOST_DIR`` 注入），据此把前缀改写
        为宿主真实路径；默认两值相同，改写恒等。只匹配 store_dir 本身或其子路径
        （无尾斜杠歧义），其余 host 键原样返回。
        """
        if not store_dir or not host_store_dir or host_store_dir == store_dir:
            return host
        base = store_dir.rstrip("/")
        if host == base or host.startswith(base + "/"):
            return host_store_dir.rstrip("/") + host[len(base) :]
        return host

    @staticmethod
    def _build_volumes(
        volumes: dict | None,
        driver_mounts: list[str] | None,
        model_store_dir: str | None = None,
        model_host_store_dir: str | None = None,
    ) -> dict | None:
        """合并 volumes（{host: container}）与 driver_mounts（["host:cont[:mode]"]）为 SDK volumes 格式。

        volumes 的 host 键先过模型宿主前缀改写（host 键是节点容器内视角的 canonical
        路径，兄弟容器需要宿主真实路径）；driver_mounts 的 src 本就是宿主路径
        （Ascend 驱动目录等），不改写。
        """
        out: dict[str, dict] = {}
        if volumes:
            for host, cont in volumes.items():
                rewritten = DockerDeployer._rewrite_model_host_path(
                    str(host), model_store_dir or "", model_host_store_dir or ""
                )
                out[rewritten] = {"bind": str(cont), "mode": "rw"}
        if driver_mounts:
            for spec_str in driver_mounts:
                parts = str(spec_str).split(":")
                if len(parts) < 2:
                    continue
                host, cont = parts[0], parts[1]
                mode = parts[2] if len(parts) > 2 else "rw"
                out[host] = {"bind": cont, "mode": mode}
        return out or None

    @staticmethod
    def _build_restart_policy(rp: str | None) -> dict | None:
        """restart_policy（str）→ SDK {"Name": ...}；不对 always/unless-stopped 带 MaximumRetryCount。"""
        if rp is None:
            return None
        return {"Name": rp}

    @staticmethod
    def _spec_to_run_kwargs(spec: DeploySpec) -> dict:
        """把 DeploySpec 字段映射到 ``client.containers.create()`` 的 kwargs。

        纯函数，不触碰 docker daemon——便于单测覆盖 gpus/ipc/extra_devices 等非直传字段。
        直接消费 DeploySpec（属性访问），返回的 kwargs dict 即 docker SDK 边界（create(**kwargs)）。
        """
        kwargs: dict[str, Any] = {
            "image": spec.image,
            "name": spec.name,
        }

        # entrypoint(list|None) / args[](→command)
        if spec.entrypoint is not None:
            kwargs["entrypoint"] = spec.entrypoint
        if spec.args:
            kwargs["command"] = spec.args

        # 直传字段
        if spec.env:
            kwargs["environment"] = spec.env
        if spec.labels:
            # labels value 必须 string（daemon 400）
            kwargs["labels"] = {k: str(v) for k, v in spec.labels.items()}
        if spec.network_mode:
            kwargs["network_mode"] = spec.network_mode
        if spec.shm_size:
            kwargs["shm_size"] = spec.shm_size
        if spec.runtime:
            kwargs["runtime"] = spec.runtime
        if spec.privileged:
            kwargs["privileged"] = True
        if spec.cap_add:
            kwargs["cap_add"] = list(spec.cap_add)

        # 非直传：重命名
        if spec.ipc:
            kwargs["ipc_mode"] = spec.ipc
        if spec.extra_devices:
            kwargs["devices"] = list(spec.extra_devices)

        # 端口：DeploySpec.ports 是 {host: cont}（与 docker_run_command 的 -p host:cont 同口径），
        # 而 SDK 的 ports kwarg 是 {container: host} —— 必须反转，照原样传会把映射方向反过来
        if spec.ports:
            kwargs["ports"] = {str(cont): int(host) for host, cont in spec.ports.items()}

        # 卷（volumes + driver_mounts 合并；模型卷 host 侧按宿主真实前缀改写，
        # model_host_store_dir 缺省时恒等，存量部署零影响）
        vol = DockerDeployer._build_volumes(
            spec.volumes,
            spec.driver_mounts,
            model_store_dir=settings.model_store_dir,
            model_host_store_dir=settings.model_host_store_dir,
        )
        if vol:
            kwargs["volumes"] = vol

        # GPU（gpus → device_requests）
        dreq = DockerDeployer._parse_gpus(spec.gpus)
        if dreq is not None:
            kwargs["device_requests"] = dreq

        # restart_policy
        rp = DockerDeployer._build_restart_policy(spec.restart_policy)
        if rp is not None:
            kwargs["restart_policy"] = rp

        # detach 固定（run/create 由调用方控制 start）
        kwargs["detach"] = True
        return kwargs

    # ---- 容器操作（asyncio.to_thread 包同步 SDK）----

    @staticmethod
    def _create_or_replace(client: docker.DockerClient, kwargs: dict) -> Any:
        """幂等创建容器：同名冲突(409)则 stop+rm 旧容器后重试。"""
        name = kwargs.get("name")
        try:
            container = client.containers.create(**kwargs)
        except APIError as e:
            if e.status_code == 409 and name:
                log.info("Container name=%s in use (409), replacing", name)
                try:
                    old = client.containers.get(name)
                    old.stop(timeout=10)
                    old.remove(force=True)
                except NotFound:
                    pass
                container = client.containers.create(**kwargs)
            else:
                raise
        container.start()
        return container

    async def deploy(self, spec: DeploySpec) -> DeployResult:
        """按 spec 启动容器（幂等：同名先 stop+rm 再 run）。返回 DeployResult。

        不回传 container_id。per-name 锁在 Deployer 层序列化同名 create/replace（模块级、
        跨实例共享），ContainerServiceImpl（RPC 路径）与执行器（直调 deploy）都受保护。
        """
        name = spec.name
        kwargs = self._spec_to_run_kwargs(spec)
        log.info("Running container name=%s image=%s", name, kwargs["image"])

        def _do() -> None:
            client = _get_client()
            self._create_or_replace(client, kwargs)

        async with _deploy_lock(name):
            try:
                await asyncio.to_thread(_do)
            except APIError as e:
                raise TaskExecutionError(f"容器启动失败（name={name}）: {e.explanation}") from e
        return DeployResult(success=True, container_name=name)

    async def stop(self, name: str, timeout: int = 10) -> StopResult:
        """停止容器。"""
        log.info("Stopping container name=%s", name)

        def _do() -> StopResult:
            client = _get_client()
            try:
                container = client.containers.get(name)
            except NotFound:
                return StopResult(success=True, container_name=name)
            container.stop(timeout=timeout)
            return StopResult(success=True, container_name=name)

        try:
            return await asyncio.to_thread(_do)
        except APIError as e:
            raise TaskExecutionError(f"容器停止失败（name={name}）: {e.explanation}") from e

    async def remove(self, name: str, force: bool = False) -> RemoveResult:
        """删除容器（rollback/恢复对 created/restarting 态传 force=True）。"""
        log.info("Removing container name=%s force=%s", name, force)

        def _do() -> RemoveResult:
            client = _get_client()
            try:
                container = client.containers.get(name)
            except NotFound:
                return RemoveResult(success=True, removed=False, container_name=name)
            container.remove(force=force)
            return RemoveResult(success=True, removed=True, container_name=name)

        try:
            return await asyncio.to_thread(_do)
        except APIError as e:
            raise TaskExecutionError(f"容器删除失败（name={name}）: {e.explanation}") from e

    async def list(self, label_filter: list[str] | None = None, all: bool = True) -> list[ContainerSummary]:
        """按标签列容器。label_filter 是 ["key=value", ...]（list 非 dict）；rollback 必须 all=True。"""
        filters = {"label": label_filter} if label_filter else None

        def _do() -> list[ContainerSummary]:
            client = _get_client()
            containers = client.containers.list(all=all, filters=filters)
            return [
                ContainerSummary(
                    name=c.name,
                    id=c.id,
                    state=c.attrs.get("State", {}).get("Status") or "",
                    labels=dict(c.labels) if c.labels else {},
                )
                for c in containers
            ]

        return await asyncio.to_thread(_do)

    async def inspect(self, name: str) -> ContainerState:
        """查容器 State（gate 用）。返回 docker State 子树（PascalCase 字段）。"""

        def _do() -> ContainerState:
            client = _get_client()
            try:
                container = client.containers.get(name)
            except NotFound:
                raise ContainerGone(f"容器不存在: {name}")
            state = container.attrs.get("State", {})
            return ContainerState(
                Status=state.get("Status", ""),
                Running=state.get("Running", False),
                Restarting=state.get("Restarting", False),
                RestartCount=state.get("RestartCount", 0),
                ExitCode=state.get("ExitCode", 0),
            )

        try:
            return await asyncio.to_thread(_do)
        except APIError as e:
            raise TaskExecutionError(f"容器状态查询失败（name={name}）: {e.explanation}") from e

    async def logs(self, name: str, tail: int = 200) -> str:
        """取容器日志尾部（引擎崩溃诊断用；stdout+stderr 合并）。

        容器已退出/被 stop 后仍可取（docker 保留至 rm）；rollback rm 前调此抓崩溃原因。
        """

        def _do() -> str:
            client = _get_client()
            try:
                container = client.containers.get(name)
            except NotFound:
                return ""  # 容器已不存在（race）→ 空日志，不阻断上报
            # logs(tail=N) 返回 bytes（含 ANSI）；stderr=True 合并 stderr
            data = container.logs(tail=tail, stdout=True, stderr=True)
            if isinstance(data, bytes):
                data = data.decode(errors="replace")
            return data or ""

        try:
            return await asyncio.to_thread(_do)
        except APIError as e:
            log.warning("docker logs failed (name=%s): %s", name, e)
            return ""

    # ---- 镜像级操作（PULL_IMAGE 任务用）----

    async def image_download_size(self, image: str) -> int | None:
        """镜像将传输的 blob 大小之和：经 registry v2 API 预读 manifest（匿名）。

        docker.io ref 优先走 daemon 配的镜像加速站（``client.info()`` 的 Mirrors，
        与 daemon 本轮拉取同源）；daemon 标 insecure 的 registry 按明文 http 探测。
        探测出的 digest→size 清单存实例状态 ``_probe_layers``（有界），供**并行进行中**
        的 ``pull_image`` 把本地已缓存层的完成态 chunk 配对补足分子——探测与拉取并行，
        清单无法经参数传递，只能 deployer 内部衔接。失败返回 None，调用方回落旧口径。
        """

        def _daemon_reg_config() -> tuple[list[str], set[str]]:
            client = _get_client()
            rc = client.info().get("RegistryConfig") or {}
            mirrors = list(rc.get("Mirrors") or [])
            insecure = {
                name
                for name, cfg in (rc.get("IndexConfigs") or {}).items()
                if isinstance(cfg, dict) and not cfg.get("Secure", True) and "/" not in name
            }
            return mirrors, insecure

        try:
            registry, _ = resolve_repository_name(image)
        except Exception:
            registry = ""
        # docker.io 才需要加速站列表；但 insecure 标记（daemon.json 的
        # insecure-registries）对私有 registry 同样生效——明文部署的私网 registry
        # 不查就恒按 https 探测必失败，预探测整体失效。两次都查 daemon info()
        # （本地 socket 往返，代价可忽略）
        mirrors, insecure = await asyncio.to_thread(_daemon_reg_config)
        if registry != "docker.io":
            mirrors = []
        blobs = await probe_image_layers(image, mirrors=mirrors, insecure_registries=insecure)
        if blobs is None:
            return None
        if len(self._probe_layers) >= self._PROBE_LAYERS_MAX:
            self._probe_layers.clear()
        known: dict[str, int] = {}
        for digest, size in blobs:
            known[digest] = size
            known.setdefault(digest[:12], size)  # stream id 是 digest 的 12 位短前缀
        total = sum(size for _, size in blobs)
        self._probe_layers[image] = ProbeEntry(aliases=known, unique_total=total)
        return total

    async def pull_image(
        self,
        image: str,
        cancel_event: asyncio.Event | None = None,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        """SDK 流式拉取镜像（低层 ``api.pull(stream=True, decode=True)``）。

        用 SDK 而非 ``docker pull`` CLI 子进程是生产拓扑硬约束：目标环境只把
        docker.sock 挂进容器、容器内没有 docker CLI——CLI 方案在生产跑不起来。
        SDK 本就是其余容器操作的主客户端，唯 pull 历史上走 CLI。

        流的打开见 :func:`open_pull_stream`（低层 API 的原因在那里说）。

        消费模式：阻塞迭代器经 ``asyncio.to_thread(next)`` 逐 chunk 跳线程——线程内
        只有 ``next()`` 一个阻塞调用（StopIteration 转哨兵，不跨线程抛出），cancel
        检查与进度回调留在事件循环侧，``cancel_event`` 仍是 ``asyncio.Event``。

        进度聚合：按 chunk 的 ``id``/``progressDetail`` 维护 layer 表（同层取 max 防
        回退），分母只计已出现层；百分比与单调守卫由上层 reporter 按「预探测总量 vs
        层聚合」取大计算，本层不重复算。完成态 chunk（Already exists 等本地缓存层）
        在预探测清单（实例状态，与拉取并行落地）里按 digest 配对补足分子——否则种入
        的总量分母到不了头。

        回调粒度按字节：累计字节有实际增量即回调（不限整百分比——45GB 镜像 1% 就是
        450MB，按百分比回调会长时间静默，上层无法算速率）；节流由上层 executor 的
        wire 节流负责，本层不做。

        认证：SDK 自动读 ``~/.docker/config.json`` 的 docker login 凭证；容器内若未
        挂载该文件，私有 registry 拉取会 401——届时按「只报 stage 不报进度」的降级
        规则处理，不退回 CLI。
        """
        log.info("pull_image: %s", image)

        layers: dict[str, tuple[int, int]] = {}
        known_blobs: dict[str, int] | None = None
        # 流消费/错误识别/落盘复核/迭代器关闭统一在 iter_pull_chunks（拉取流的
        # 正确性契约见其 docstring），本方法只做进度聚合与收尾补报
        async for chunk in iter_pull_chunks(image, cancel_event):
            if on_progress is None:
                continue
            # 并行探测的清单惰性衔接：首个完成态 chunk 到来时取一次（此后固定）。
            # 存值 ProbeEntry 的 aliases/unique_total 分工见其 docstring
            if known_blobs is None and isinstance(chunk, dict) and chunk.get("status") in _COMPLETE_STATUSES:
                entry = self._probe_layers.get(image)
                known_blobs = entry.aliases if entry is not None else None
            done, total = _update_pull_layers(layers, chunk, known_blobs)
            if total <= 0:
                continue
            await on_progress(done, total)
        # 本地缓存的层这个 daemon 不发任何进度行，中途分子到不了种入的总量（部分缓存
        # 拉取会长时间停在低百分比）——收尾补一报到顶，让时间线终结在「41.1MB/41.1MB」
        # 而不是「14.7MB/41.1MB → 成功」的悬空感
        if on_progress is not None:
            entry = self._probe_layers.get(image)
            if entry is not None:
                done, total = _sum_layers(layers)
                # 总量用探测存的唯一 blob 总和——配对表 full digest 与 12 位前缀别名
                # 并存，按其值求和会把每个 blob 双计（实测 8.4GB 镜像收尾显示 16.8GB）
                final = max(done, total, entry.unique_total)
                if final > 0:
                    await on_progress(final, final)
        log.info("pull_image complete: %s", image)

    async def image_exists(self, image: str) -> bool:
        """镜像是否已在本节点（executor PULL 短路用：不存在/daemon 错误均视为需拉取）。

        与 RPC ``NodeServiceImpl.image_exists``（daemon 错误上抛，manager 复核用）
        的语义差异是有意的：本方法只做本地短路，探测不了就交给 pull 流程暴露真因。
        """
        return await asyncio.to_thread(_image_exists_swallowing, image)
