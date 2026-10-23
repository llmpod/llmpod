"""ResourceLedger：进程内资源乐观预留（GPU 显存 + 节点内存）。

与 GpuStatusBuffer/SystemBuffer（心跳镜像，反映实际占用）分离——ResourceLedger 只记 Manager
编排级的「预留」（部署前占位），是乐观锁；持久化权威来源是 DeploymentGpuAssignment 表（显存）
与 Instance.spec（内存 claim）。scheduler 读 get_available 做全局校验，executor 成功后写表，
rollback 时 release + 删表行。

两个维度各自成账，同一把锁保护：

- 显存：``deployment_id → gpu_uuid → GB``（与历史口径一致，容量 = 卡 total × util）；
- 内存：``deployment_id → node_id → GB``（CPU/混合 offload 的权重与 KV 落内存部分，
  容量 = 节点 memory_total × util）。
"""

from __future__ import annotations

import asyncio
import logging

from llmpod.apps.manager.deployment.resource import DEFAULT_HEADROOM

log = logging.getLogger("llmpod.apps.manager.deployment.resource_ledger")

# 默认利用率上限（0.9，留 10% 余量给框架/CUDA context/页缓存）——账本/引擎默认/持久化
# 缺省值的唯一定义处：数值由 :mod:`resource` 持有（测算与账本共用同一 headroom 策略）。
DEFAULT_GPU_MEMORY_UTILIZATION = DEFAULT_HEADROOM
# 内存侧同值口径（与显存共用 headroom 策略，改一处同步）
DEFAULT_RAM_UTILIZATION = DEFAULT_GPU_MEMORY_UTILIZATION


class ResourceLedger:
    """进程内资源预留账本（显存按 gpu_uuid、内存按 node_id，per deployment）。"""

    def __init__(self) -> None:
        # deployment_id -> {gpu_uuid: reserved_gb}
        self._reserved: dict[str, dict[str, float]] = {}
        # deployment_id -> {node_id: reserved_gb}
        self._ram_reserved: dict[str, dict[str, float]] = {}
        # 增量总和（gpu_uuid / node_id -> 全部部署的预留之和）：get_available* 在调度循环里
        # 被逐卡逐节点调用，每次遍历全部部署是 O(卡数 × 部署数)。这四个入口是唯一写点，
        # 记账时同步加减——别处不要直接改上面两个 dict。
        self._gpu_totals: dict[str, float] = {}
        self._ram_totals: dict[str, float] = {}
        self._guard = asyncio.Lock()

    def _add_total(self, totals: dict[str, float], key: str, delta: float) -> None:
        """增减增量总和（归零即删键，避免长跑后残留一堆 0）。"""
        left = totals.get(key, 0.0) + delta
        if left > 1e-9:
            totals[key] = left
        else:
            totals.pop(key, None)

    # ---- 显存 ----

    def _gpu_reserved(self, gpu_uuid: str) -> float:
        """某 GPU 上所有部署的预留总和（O(1)，读增量总和）。"""
        return self._gpu_totals.get(gpu_uuid, 0.0)

    async def reserve(
        self,
        deployment_id: str,
        gpu_uuid: str,
        vram_gb: float,
        total_gb: float,
        util: float = DEFAULT_GPU_MEMORY_UTILIZATION,
    ) -> bool:
        """预留显存（乐观）。校验 used+req ≤ total*util，成功**累加**记账返回 True。

        同一 (部署, 卡) 重复预留会累加（与内存维度同一语义）——调用方重规划前先 ``release``。
        """
        if vram_gb <= 0:
            return True
        async with self._guard:
            capacity = total_gb * util
            already = self._gpu_reserved(gpu_uuid)
            if already + vram_gb > capacity + 1e-9:
                log.warning(
                    "VRAM reserve failed: deployment=%s gpu=%s need=%.2f used=%.2f cap=%.2f",
                    deployment_id[:8],
                    gpu_uuid,
                    vram_gb,
                    already,
                    capacity,
                )
                return False
            claims = self._reserved.setdefault(deployment_id, {})
            claims[gpu_uuid] = claims.get(gpu_uuid, 0.0) + vram_gb
            self._add_total(self._gpu_totals, gpu_uuid, vram_gb)
            return True

    async def force_reserve(
        self,
        deployment_id: str,
        gpu_uuid: str,
        vram_gb: float,
    ) -> None:
        """强制预留显存（不计容量校验，直接记账，同样累加）。

        start 端点 force 启动时用：原卡已被别的部署占用、用户显式同意抢占时，仍记账本部署
        预留（ledger 可能超额，由用户承担 OOM 风险）。与 ``reserve`` 同一记账格式，仅跳过
        ``used+req ≤ cap`` 校验。
        """
        if vram_gb <= 0:
            return
        async with self._guard:
            claims = self._reserved.setdefault(deployment_id, {})
            claims[gpu_uuid] = claims.get(gpu_uuid, 0.0) + vram_gb
            self._add_total(self._gpu_totals, gpu_uuid, vram_gb)

    async def release(self, deployment_id: str) -> None:
        """释放某部署的全部预留（显存 + 内存；rollback/停止/删除时调）。"""
        async with self._guard:
            for gpu_uuid, gb in (self._reserved.pop(deployment_id, None) or {}).items():
                self._add_total(self._gpu_totals, gpu_uuid, -gb)
            for node_id, gb in (self._ram_reserved.pop(deployment_id, None) or {}).items():
                self._add_total(self._ram_totals, node_id, -gb)

    async def release_cards(self, deployment_id: str, gpu_uuids) -> None:
        """释放某部署在指定卡上的预留（部分释放：在线缩容删份 / 扩容失败回滚新份）。

        只动列出的卡，其余预留原样保留——与 :meth:`release` 的全量语义互补。
        """
        uuids = set(gpu_uuids)
        if not uuids:
            return
        async with self._guard:
            gpus = self._reserved.get(deployment_id)
            if not gpus:
                return
            for u in uuids:
                removed = gpus.pop(u, None)
                if removed:
                    self._add_total(self._gpu_totals, u, -removed)
            if not gpus:
                self._reserved.pop(deployment_id, None)

    def get_available(self, gpu_uuid: str, total_gb: float, util: float = DEFAULT_GPU_MEMORY_UTILIZATION) -> float:
        """某 GPU 的账本可用显存 = total*util - 已预留。

        只管平台自己的**预留**（含已规划未起的 staging 部署），不含心跳 used_vram——心跳
        是「此刻实况」（含平台外进程），由 ``GpuScheduler.card_fits`` 的物理可用闸另行判
        一次。两条闸门互补：账本看得见「已规划未起」，心跳看得见「别人已经占着」。
        """
        return max(0.0, total_gb * util - self._gpu_reserved(gpu_uuid))

    def get_reserved_for_deployment(self, deployment_id: str) -> dict[str, float]:
        """查某部署的显存预留明细（写 DeploymentGpuAssignment 用）。"""
        return dict(self._reserved.get(deployment_id, {}))

    # ---- 内存（CPU / 混合 offload 的权重与 KV 落内存部分）----

    def _ram_reserved_on(self, node_id: str) -> float:
        """某节点上所有部署的内存预留总和（O(1)，读增量总和）。"""
        return self._ram_totals.get(node_id, 0.0)

    async def reserve_ram(
        self,
        deployment_id: str,
        node_id: str,
        ram_gb: float,
        total_gb: float,
        util: float = DEFAULT_RAM_UTILIZATION,
    ) -> bool:
        """预留节点内存（乐观）。校验 used+req ≤ total*util，成功**累加**记账返回 True。

        同节点承载同部署的多份（CPU 部署 binpack 常态）必须累加——覆盖写会让账本少记，
        可用内存虚高、后续部署被放进来导致 OOM。调用方需保证「重新规划前先 release」。
        """
        if ram_gb <= 0:
            return True
        async with self._guard:
            capacity = total_gb * util
            already = self._ram_reserved_on(node_id)
            if already + ram_gb > capacity + 1e-9:
                log.warning(
                    "RAM reserve failed: deployment=%s node=%s need=%.2f used=%.2f cap=%.2f",
                    deployment_id[:8],
                    node_id,
                    ram_gb,
                    already,
                    capacity,
                )
                return False
            claims = self._ram_reserved.setdefault(deployment_id, {})
            claims[node_id] = claims.get(node_id, 0.0) + ram_gb
            self._add_total(self._ram_totals, node_id, ram_gb)
            return True

    async def force_reserve_ram(self, deployment_id: str, node_id: str, ram_gb: float) -> None:
        """强制预留内存（不计容量校验；与 :meth:`force_reserve` 同语义，同样累加）。"""
        if ram_gb <= 0:
            return
        async with self._guard:
            claims = self._ram_reserved.setdefault(deployment_id, {})
            claims[node_id] = claims.get(node_id, 0.0) + ram_gb
            self._add_total(self._ram_totals, node_id, ram_gb)

    async def release_ram_nodes(self, deployment_id: str, amounts: dict[str, float]) -> None:
        """按量释放某部署在指定节点上的内存预留（部分释放：缩容删份 / 扩容回滚）。

        ``amounts`` 为 ``{node_id: 释放量}``——同节点可能承载同部署的多份，按量扣减而非整节点清空。
        """
        if not amounts:
            return
        async with self._guard:
            nodes = self._ram_reserved.get(deployment_id)
            if not nodes:
                return
            for node_id, gb in amounts.items():
                before = nodes.get(node_id, 0.0)
                left = before - float(gb)
                # 增量总和按**实际**扣减量走（请求量可能大于剩余 claim，按请求量扣会漂移成负数）
                self._add_total(self._ram_totals, node_id, -min(before, float(gb)))
                if left > 1e-9:
                    nodes[node_id] = left
                else:
                    nodes.pop(node_id, None)
            if not nodes:
                self._ram_reserved.pop(deployment_id, None)

    def get_available_ram(self, node_id: str, total_gb: float, util: float = DEFAULT_RAM_UTILIZATION) -> float:
        """某节点可用内存 = total*util - 已预留（不含心跳 used，那是节点实况）。"""
        return max(0.0, total_gb * util - self._ram_reserved_on(node_id))

    def get_ram_reserved_for_deployment(self, deployment_id: str) -> dict[str, float]:
        """查某部署的内存预留明细（node_id → GB）。"""
        return dict(self._ram_reserved.get(deployment_id, {}))

    def has_reservation(self, deployment_id: str) -> bool:
        """该部署是否还有账本残留（显存或内存任一维度）。

        收敛判定用：stop/failed 稳态若仍有残留（如纯 CPU 部署无 assignment 行，
        收敛快路径可能没走到 release），必须落下去释放而不是直接 return。
        """
        return bool(self._reserved.get(deployment_id) or self._ram_reserved.get(deployment_id))

    # ---- 重建 ----

    async def rebuild_from_db(self) -> None:
        """从持久化权威来源重建内存预留（manager 重启后恢复预留态）。

        显存读 ``DeploymentGpuAssignment`` 表；内存读 ``Instance`` 行（``spec.ram_claim_gb``
        按 ``assigned_node`` 求和——纯 CPU 实例没有 GPU 分配行、KV 落内存的显存实例虽有
        分配行但内存侧只记在 spec，Instance.spec 是内存预留唯一的持久化载体）。只统计仍属
        活跃部署的行：stop 只释放预留、不删 Instance 行。
        """
        await self._rebuild_vram_from_db()
        await self._rebuild_ram_from_db()

    async def _rebuild_vram_from_db(self) -> None:
        from sqlalchemy import select as _select

        from llmpod.db import get_db_context
        from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment

        async with self._guard:
            self._reserved.clear()
            self._gpu_totals.clear()
            async with get_db_context() as db:
                for a in (await db.execute(_select(DeploymentGpuAssignment))).scalars():
                    self._reserved.setdefault(a.deployment_id, {})[a.gpu_uuid] = float(a.allocated_vram_gb or 0)
            for gpus in self._reserved.values():
                for gpu_uuid, gb in gpus.items():
                    self._add_total(self._gpu_totals, gpu_uuid, gb)
        log.info("ResourceLedger VRAM rebuilt from DB: %d deployment(s)", len(self._reserved))

    async def _rebuild_ram_from_db(self) -> None:
        from sqlalchemy import select as _select

        from llmpod.apps.manager.deployment.constants import DEPLOYMENT_ACTIVE_STATUSES
        from llmpod.db import get_db_context
        from llmpod.models.instance import Instance
        from llmpod.models.model_deployment import ModelDeployment

        async with self._guard:
            self._ram_reserved.clear()
            self._ram_totals.clear()
            async with get_db_context() as db:
                active_ids = {
                    d.deployment_id
                    for d in (
                        await db.execute(
                            _select(ModelDeployment).where(ModelDeployment.status.in_(DEPLOYMENT_ACTIVE_STATUSES))
                        )
                    ).scalars()
                }
                # 只取活跃部署的行：Instance 行在 stop 后仍保留，全表扫描会把历史行连 spec JSONB
                # 一起拉出来再丢弃（集群跑久了这张表很大）。
                rows = (
                    (await db.execute(_select(Instance).where(Instance.deployment_id.in_(list(active_ids))))).scalars()
                    if active_ids
                    else ()
                )
                by_deployment: dict[str, list] = {}
                for inst in rows:
                    by_deployment.setdefault(inst.deployment_id, []).append(inst)
                for dep_id, insts in by_deployment.items():
                    claims = ram_claims_by_node(insts)
                    if claims:
                        self._ram_reserved[dep_id] = claims
            for nodes in self._ram_reserved.values():
                for node_id, gb in nodes.items():
                    self._add_total(self._ram_totals, node_id, gb)
        log.info("ResourceLedger RAM rebuilt from DB: %d deployment(s)", len(self._ram_reserved))


def ram_claims_by_node(instances) -> dict[str, float]:
    """把分片的 ``spec.ram_claim_gb`` 按 ``assigned_node`` 汇总（``{node_id: GB}``）。

    **内存 claim 的唯一聚合点**：账本重建（进程重启恢复预留态）与部分释放（缩容摘份、
    扩容回滚）要做的是同一件事——字段名与「0/缺失不计」规则各写一份，claim 结构一改就
    会分叉（重建多记、释放少还，重启后账本与真实占用对不上）。
    """
    out: dict[str, float] = {}
    for inst in instances or ():
        node_id = getattr(inst, "assigned_node", None)
        claim = float((getattr(inst, "spec", None) or {}).get("ram_claim_gb") or 0.0)
        if node_id and claim > 0:
            out[node_id] = out.get(node_id, 0.0) + claim
    return out
