"""Executor 契约：声明式任务的具体执行 ABC（domain-neutral 扩展点）。

registry（``NodeAgent._executors``）按 ``task.type`` 派发；新增任务类型 = 新增 ``Executor``
子类 + 注册到 registry。``NodeAgent`` 只认 ``registry.get(type)``，未知 type → FAILED
``无执行器 type=…``。

双方法契约：

- ``execute``：驱动任务到终态；**要 secrets**（内存，随 ``ensure_task`` 传入，终态即清）。终态
 经 ``report`` 回填——execute 本身不返回 status。
- ``reconcile``：**无 secrets** 的重启探测（允许轻量只读副作用，如容器 inspect / 端口探活）。
  ``NodeAgent._recover`` 对在途任务调用，返回已达成的终态则直接上报，返回 None 则视为
  「重启中断 → FAILED 等重发」。默认 None（多数类型若无廉价探测可不覆写）。async——
  容器类执行器的存活/健康判定需要 I/O（docker inspect / HTTP 探活）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from llmpod.apps.node.models import LocalTask
from llmpod.config.constants import TaskStatus

log = logging.getLogger("llmpod.apps.node.executors.base")

RecipeModel = TypeVar("RecipeModel", bound=BaseModel)


def parse_payload(task: LocalTask, cls: type[RecipeModel]) -> RecipeModel | None:
    """把 ``task.payload``（JSONB 自由载荷）校验为 typed 配方模型。

    不合约返回 ``None`` 并告警（含校验错误详情）——失败语义（execute 报 FAILED /
    reconcile 视为未达成）由调用方决定，解析与告警在此收敛为单一实现。
    """
    try:
        return cls.model_validate(task.payload or {})
    except ValidationError as e:
        log.warning("配方校验失败 task=%s type=%s: %s", task.task_id[:8], task.type, e)
        return None


# report 回调契约：async def report(status: str, progress: int = 0, *,
#                                  stage: str | None = None,
#                                  message: str | None = None,
#                                  instance_status: str | None = None,
#                                  container_id: str | None = None,
#                                  endpoint: str | None = None,
#                                  extra: dict | None = None) -> None
# - status：第一层生命周期（TaskStatus 封闭 5 值）
# - stage：第二层子阶段（各任务类型词表成员，RUNNING 期间推进；终态报 FINISHED 族值）
# - progress：第三层辅助量，仅 0-100 int——无量任务（如启停容器）不传，保持 0
# - instance_status/container_id/endpoint：结构化标记载荷——instance_status 触发
#   ``report_instance_status`` 独立通道（驱动 instance 表与部署 phase 推进），key
#   漂移会静默丢失上报，故为具名参数而非 extra dict 键
# - extra：自由细节审计轨迹（bytes_done/model_path 等冻结键，不含 secrets）
ReportFn = Callable[..., Awaitable[None]]


class ByteProgressReporter:
    """下载字节进度上报节流器：把「每 chunk 的累计字节」折算成节流后的 ``report``。

    下载器每个 chunk 都能拿到累计字节，逐 chunk 上报会刷爆上报通道与 deploy_log；本类按
    「距上次放行 ≥``min_interval`` 秒」放行（纯时间节拍——停滞期也按节拍发心跳，活跃期
    字节增量天然反映在 bytes_done 里，无需再按 Δ 双重闸）。终值 100 与阶段迁移由调用方
    另报（不受节流）。``force=True`` 无条件放行：文件边界「已落定」检查点用，绕过节拍
    但不污染速率。

    镜像拉取与模型下载（HF / local 源）共用本类，行为一致：

    - ``bytes_total`` 与历史值取大：镜像拉取在构造时种入预探测总量（manifest 全部
      layer 压缩和 + config/attestation blob），下载中传入的「已出现层聚合」只增不减；
      超过探测值时长大并即刻放行一报（用户可见分母更新），百分比对历史最大值取 max
      （分母增大时百分比短暂回退，时间线进度条不许倒走）；
    - 第二次放行起 ``extra`` 携带 ``bytes_per_sec``（对上次放行点算瞬时速率、EMA 平滑）。
      **速率只认真实传输**：``force`` 检查点（文件跳过/边界对账）与过短窗口
      （<``_RATE_MIN_WINDOW``，分母增长连击）不进 EMA，只平移基线——否则一次跳档
      会把「已下载 18GB（6.2GB/s）」这种鬼数字留在时间线上等 EMA 慢慢衰完；
    - ``context`` 进 ``extra``（如 ``{"image": "vllm/..."}``），调用方自带识别字段。

    多文件下载的基座由**调用方**持有（文件开始时的累计字节），轮询报 ``base + 本文件
    已下``——reporter 不主动提供跨文件基座参与运算（避免「把已含本文件进度的累计
    再加一遍」的双计），``last_bytes_done`` 只读属性仅供衔接检查点（并行探测种子）。

    只在事件循环内调用：``_manager`` 的流式循环直接 await；``_hf`` 的进度来自工作线程的
    tqdm 回调，由轮询协程转述后同样落到这里（避免跨线程调度协程）。
    """

    # 速率计算的最小窗口（秒）：小于此窗口的放行只平移基线不进 EMA
    _RATE_MIN_WINDOW = 0.5

    def __init__(
        self,
        report: ReportFn,
        *,
        bytes_total: int | None,
        stage: str,
        context: dict | None = None,
        min_interval: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._report = report
        self._total = int(bytes_total) if bytes_total else None
        self._stage = stage
        self._context = dict(context) if context else None
        self._min_interval = min_interval
        self._clock = clock or time.monotonic
        self._last_pct = -1
        self._last_at: float | None = None
        self._last_done = 0
        self._last_done_seen = 0  # 累计字节最大已见值（含被节流丢弃的报点）
        self._rate_ema: float | None = None  # 无前一放行点时不带速率字段

    async def report(self, bytes_done: int, bytes_total: int | None = None, force: bool = False) -> None:
        """按节拍上报一次（未过闸则静默返回）。

        ``bytes_total`` 可选传入（与历史值取大）：镜像拉取传「已出现层聚合」，构造时
        种入的预探测总量不会被部分和缩小；层和超过探测值时长大并即刻放行一报。
        ``force`` 跳过节拍（文件边界检查点）。
        """
        done = max(0, int(bytes_done))
        self._last_done_seen = max(self._last_done_seen, done)
        grew = bool(bytes_total) and (self._total is None or bytes_total > self._total)
        if grew:
            self._total = int(bytes_total)
        total = self._total
        pct = int(done / total * 100) if total else 0
        pct = max(0, min(pct, 99))  # 终值 100 由调用方在全部文件落定后上报
        now = self._clock()
        # 首报恒放行；grew（分母增长）即刻放行——用户可见分母更新不受节拍；force
        # 同理。分子追平分母是终态检查点（镜像拉取的收尾补报到顶），同样放行——
        # 否则收尾一报被节拍吞掉，时间线停在低百分比直接跳终态
        reached_top = bool(total) and done >= total
        if (
            not grew
            and not force
            and not reached_top
            and self._last_at is not None
            and now - self._last_at < self._min_interval
        ):
            return
        extra: dict = {"bytes_done": done, "bytes_total": self._total}
        if self._context:
            extra.update(self._context)
        if self._last_at is not None:
            dt = now - self._last_at
            # 速率只认真实传输：force 检查点、分母刚长大（grew 连击毫秒级窗口）、
            # 终态跳满（缓存层补报到顶）与过短窗口只平移基线，不进 EMA——整段
            # 跳过/续传瞬间 +18GB 不会虚增出「6.2GB/s」
            if not force and not grew and not reached_top and dt >= self._RATE_MIN_WINDOW:
                inst = (done - self._last_done) / dt
                self._rate_ema = inst if self._rate_ema is None else 0.3 * inst + 0.7 * self._rate_ema
        if self._rate_ema is not None:
            extra["bytes_per_sec"] = int(self._rate_ema)
        self._last_pct = max(pct, self._last_pct)  # 单调守卫
        self._last_at = now
        self._last_done = done
        await self._report(
            TaskStatus.RUNNING,
            self._last_pct,
            stage=self._stage,
            extra=extra,
        )

    @property
    def last_bytes_done(self) -> int:
        """累计字节的最大已见值（含被节流丢弃的报点）——并行探测落地时衔接当前进度用。"""
        return self._last_done_seen


@dataclass
class ReconcileResult:
    """重启恢复探测结果（``reconcile`` 返回）：已达成的终态。

    结构化标记载荷与 ``report`` 的具名参数一一对应；``extra`` 为自由审计键。
    """

    status: str  # 终态，如 "SUCCESS"
    progress: int = 100
    instance_status: str | None = None
    container_id: str | None = None
    endpoint: str | None = None
    extra: dict = field(default_factory=dict)


class Executor(ABC):
    """任务执行器契约（registry 唯一扩展点）。"""

    @abstractmethod
    async def execute(
        self,
        task: LocalTask,
        secrets: dict | None,
        report: ReportFn,
        cancel_event: asyncio.Event,
    ) -> None:
        """驱动任务到终态（SUCCESS/FAILED/CANCELLED），全程经 ``report`` 上报。"""

    async def reconcile(self, task: LocalTask) -> ReconcileResult | None:
        """无 secrets 的重启探测：返回已达成终态或 None（未达成/需重发）。

        默认 None——无可廉价探测的终态时可不覆写，``_recover`` 据此判「重启中断」。
        """
        return None

    async def on_cancel(self, task: LocalTask) -> None:
        """任务协程被真正取消（CancelledError）时的收尾清理钩子。

        覆写方只清自己管辖的中间态（如下载的 .part 残留）；默认无副作用。
        阻塞的磁盘操作（rglob/rmtree）应包 ``asyncio.to_thread``，取消路径
        同样不能卡事件循环。
        """
        return None
