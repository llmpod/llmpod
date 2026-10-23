"""PULL_IMAGE 执行器：Node 自治拉取镜像。

声明式语义——Manager 只下发镜像 ref，Node 负责「镜像在位」：
- ``image_exists`` → 直接 SUCCESS（重复下发/重启恢复短路，同 ENSURE_MODEL 的 check-existing）；
- 不在位 → SDK 流式拉取（``cancel_event`` 可中断），逐 layer 聚合出真实进度。

进度上报与模型下载共用 :class:`ByteProgressReporter`：节流最快 2s 一条，
``extra`` 带 ``image``/``bytes_done``/``bytes_total``/``bytes_per_sec``（EMA 速率）。
总量预探测（manifest 全部 blob 大小之和）与拉取**并行**，落地后经分母取大换入。
projector 侧另有 Δ≥5%/5s 节流管库内 deploy_log 行数——双层节流各管各的资源。
PULL_IMAGE 的 resource_type 非 MODEL_PLACEMENT，Manager 不投影 node_model。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from docker.errors import APIError as _DockerAPIError

from llmpod.apps.node.deployers.base import Deployer
from llmpod.apps.node.exceptions import TaskCancelled, TaskExecutionError
from llmpod.apps.node.executors.base import ByteProgressReporter, Executor, ReconcileResult, ReportFn, parse_payload
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import PullImagePayload
from llmpod.config.constants import PullImageStage, TaskStatus

log = logging.getLogger("llmpod.apps.node.executors.pull_image")

# 总量预探测的整体 deadline（秒）：逐镜像站串行 + 每请求 connect 5s/read 10s，
# 站点全挂时不封顶能把拉取顶住几十秒——纯增强不值得等
_PROBE_DEADLINE_SECONDS = 10.0


class PullImageExecutor(Executor):
    """PULL_IMAGE 执行器（构造注入 Deployer：image_exists/pull_image 的 runtime 实现）。"""

    def __init__(self, deployer: Deployer):
        self._deployer = deployer

    async def execute(
        self,
        task: LocalTask,
        secrets: dict | None,
        report: ReportFn,
        cancel_event: asyncio.Event,
    ) -> None:
        """驱动到终态；image 缺失/拉取失败 → FAILED，cancel → CANCELLED。"""
        payload = parse_payload(task, PullImagePayload)
        if payload is None:
            await report(TaskStatus.FAILED, message="PULL_IMAGE 配方无效")
            return
        image = payload.image.strip()
        if not image:
            await report(TaskStatus.FAILED, message="PULL_IMAGE 缺少 image ref")
            return

        # 已存在短路（重复下发/重启恢复；image_exists 探测异常不阻断，交给 pull 暴露真因）
        try:
            exists = await self._deployer.image_exists(image)
        except Exception as e:
            log.warning("pull_image %s: image_exists 探测异常（继续拉取）: %s", task.task_id[:8], e)
            exists = False
        if exists:
            log.info("pull_image %s: image %s 已在位 → SUCCESS", task.task_id[:8], image)
            await report(TaskStatus.SUCCESS, 100, stage=PullImageStage.FINISHED, extra={"image": image})
            return

        # cancel checkpoint（拉取前）
        if cancel_event.is_set():
            await report(TaskStatus.CANCELLED, message="任务已取消")
            return

        await report(TaskStatus.RUNNING, stage=PullImageStage.PULLING, extra={"image": image})

        # 进度分母预探测：registry manifest 的全部 blob 大小之和（daemon 进度流无总量
        # 字段、layer 逐步入队，聚合分母是长大的下界）。**与拉取并行**——探测不阻塞流
        # 的打开，结果落地时经 reporter 的分母取大机制换成真实总量；deadline 封顶
        # （站点全挂时逐请求超时累加），失败/超时 → 全程按已出现层聚合
        probe_task = asyncio.create_task(
            asyncio.wait_for(self._deployer.image_download_size(image), timeout=_PROBE_DEADLINE_SECONDS)
        )
        reporter = ByteProgressReporter(
            report,
            bytes_total=None,
            stage=PullImageStage.PULLING,
            context={"image": image},
        )

        async def _seed_probe_total() -> None:
            """探测结果落地：有值则按「分母取大」补一报，此后的百分比即按真实总量算。"""
            try:
                probed = await probe_task
            except Exception as e:
                log.warning("pull_image %s: 总量预探测失败（按已出现层聚合）: %s", task.task_id[:8], e)
                return
            if probed:
                log.info("pull_image %s: image %s 预探测压缩总量 %.2fGB", task.task_id[:8], image, probed / 2**30)
                # done 取 reporter 已见的最大累计——探测并行期间可能已有进度
                await reporter.report(reporter.last_bytes_done, probed)

        seed_task = asyncio.create_task(_seed_probe_total())
        try:
            await self._deployer.pull_image(image, cancel_event, on_progress=reporter.report)
        except TaskCancelled:
            await report(TaskStatus.CANCELLED, message="任务已取消")
            return
        except TaskExecutionError as e:
            await report(TaskStatus.FAILED, message=f"镜像拉取失败: {e}")
            return
        except Exception as e:
            await report(TaskStatus.FAILED, message=f"镜像拉取异常: {e}")
            return
        finally:
            # 拉取先结束（秒完或探测比拉取慢）就不再等种子——探测任务自带 deadline；
            # probe_task 一并取消并回收，否则快拉场景它独自挂在 registry 请求上，
            # 超时异常无人认领（GC 时刷 "exception was never retrieved"）
            seed_task.cancel()
            probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.gather(seed_task, probe_task, return_exceptions=True)

        log.info("pull_image %s: image %s 拉取完成", task.task_id[:8], image)
        await report(TaskStatus.SUCCESS, 100, stage=PullImageStage.FINISHED, extra={"image": image})

    async def reconcile(self, task: LocalTask) -> ReconcileResult | None:
        """重启探测：镜像已在位 → SUCCESS（无副作用，不重新拉取）；否则 None（等重新下发）。

        探测异常（daemon 瞬时不可达）**上抛**而非返 None——``NodeAgent._recover``
        逐行容错，异常行本轮跳过、保持 active 留待下次 sync 重试；返 None 会被
        判「重启中断」FAILED，白烧一个 manager attempt（把瞬态当终态）。
        ``Deployer.image_exists`` 已把 docker APIError 塌缩成 False（本地短路口径），
        这里把 APIError 一并视为「探测通道不可用」上抛，保持本方法的语义不变。
        """
        payload = parse_payload(task, PullImagePayload)
        if payload is None:
            return None
        image = payload.image.strip()
        if not image:
            return None
        try:
            present = await self._deployer.image_exists(image)
        except _DockerAPIError:
            raise  # daemon 瞬时不可用：跳过本轮，不当「重启中断」FAILED
        if present:
            return ReconcileResult(TaskStatus.SUCCESS, extra={"image": image})
        return None
