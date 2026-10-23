"""reconciler 永久错误 fail-fast 回归：catalog miss / 引擎配置不支持不再无限重试。

修前：_step_pull_image 的 resolve_image miss 抛 LLMPodValidationError 冒到 controller，
每轮 scan 重跑同一步骤无限刷日志。修后：Layer 1（PULL 镜像解析）与 Layer 2
（advance_deployment 兜底）捕获永久错误 → fail_deployment 终态化；瞬时错误（DB/RPC）
照常冒给 controller 走重试。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment import reconciler
from llmpod.apps.manager.deployment.constants import DeploymentStatus
from llmpod.apps.manager.deployment.task_ops import pull_resource_key
from llmpod.core.exceptions import LLMPodValidationError, NotFound
from llmpod.models.deploy_log import DeployLog
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.task import Task


def _dep(phase: str) -> ModelDeployment:
    return ModelDeployment(
        deployment_id="dep-test-1234",
        cluster_id=1,
        name="t",
        model_id=1,
        engine="vllm",
        status=DeploymentStatus.CREATING,
        phase=phase,
        spec={},
    )


class _CtxSession:
    """advance_deployment 只 db.get(ModelDeployment)——最小 session 桩。"""

    def __init__(self, obj):
        self._obj = obj

    async def get(self, cls, pk):
        return self._obj if isinstance(self._obj, cls) else None


class TestPullImageResolveFail:
    """Layer 1：PULL_IMAGE 镜像解析 miss → fail_deployment（可操作原因），不再冒异常。"""

    async def test_catalog_miss_fails_deployment(self, monkeypatch):
        dep = _dep("PULL_IMAGE")
        inst = Instance(
            instance_id="inst-1",
            deployment_id="dep-test-1234",
            role="full",
            rank=0,
            assigned_node="node-a",
            status="PENDING",
            spec={"instance_id": "inst-1", "rank": 0, "node_rank": 0, "gpu_arch": "nvidia"},
        )
        errors: list[str | None] = []
        # engine_version 指一个 catalog 里不存在的版本 → resolve_image_ref 必 miss
        cfg = reconciler.DeploymentConfig(image_tag="99.99.0")

        async def _fake_load(deployment_id):
            return dep, None, cfg, [inst]

        async def _fake_fail(deployment_id, error=None):
            errors.append(error)

        monkeypatch.setattr(reconciler, "get_db_context", lambda: _Ctx(dep))
        monkeypatch.setattr(reconciler, "_load_context", _fake_load)
        monkeypatch.setattr(reconciler, "fail_deployment", _fake_fail)

        await reconciler.advance_deployment(dep.deployment_id)

        assert len(errors) == 1
        assert errors[0] is not None and errors[0].startswith("镜像解析失败")
        assert "vllm" in errors[0]  # 可操作原因含引擎/版本信息


class TestAdvanceCatchAll:
    """Layer 2：START 阶段永久错误（build_run_spec 路径）兜底；瞬时错误照常冒。"""

    async def test_start_head_permanent_error_fails(self, monkeypatch):
        dep = _dep("START_HEAD")
        errors: list[str | None] = []

        async def _boom(deployment_id):
            raise LLMPodValidationError("推理引擎镜像暂无 vllm/nvidia 支持性")

        async def _fake_fail(deployment_id, error=None):
            errors.append(error)

        monkeypatch.setattr(reconciler, "get_db_context", lambda: _Ctx(dep))
        monkeypatch.setattr(reconciler, "_step_start_head", _boom)
        monkeypatch.setattr(reconciler, "fail_deployment", _fake_fail)

        await reconciler.advance_deployment(dep.deployment_id)

        assert len(errors) == 1
        assert errors[0] is not None and "LLMPodValidationError" in errors[0]

    async def test_deleted_model_not_found_fails(self, monkeypatch):
        """部署引用的 Model 行被删（悬空引用）→ NotFound 视为永久错误，fail 而非每轮重试。

        修前：NotFound 不在兜底清单，冒到 controller 每轮 scan 无限刷
        ``NotFound('模型不存在: 1')``；修后：fail_deployment 终态化，后续由
        controller 对 failed 部署的收敛兜底清理 VRAM/容器。
        """
        dep = _dep("START_HEAD")
        errors: list[str | None] = []

        async def _boom(deployment_id):
            raise NotFound("Model", 1)

        async def _fake_fail(deployment_id, error=None):
            errors.append(error)

        monkeypatch.setattr(reconciler, "get_db_context", lambda: _Ctx(dep))
        monkeypatch.setattr(reconciler, "_step_start_head", _boom)
        monkeypatch.setattr(reconciler, "fail_deployment", _fake_fail)

        await reconciler.advance_deployment(dep.deployment_id)

        assert len(errors) == 1
        assert errors[0] is not None and "NotFound" in errors[0]
        assert "模型不存在: 1" in errors[0]

    async def test_transient_error_still_propagates(self, monkeypatch):
        """DB/RPC 等瞬时异常不在兜底范围内——冒给 controller 走重试语义。"""
        dep = _dep("START_HEAD")

        async def _boom(deployment_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(reconciler, "get_db_context", lambda: _Ctx(dep))
        monkeypatch.setattr(reconciler, "_step_start_head", _boom)

        with pytest.raises(RuntimeError, match="db down"):
            await reconciler.advance_deployment(dep.deployment_id)


class _Ctx:
    """get_db_context() 的最小替身：async with 进出，get 返回预置 dep。"""

    def __init__(self, dep):
        self._dep = dep

    async def __aenter__(self):
        return _CtxSession(self._dep)

    async def __aexit__(self, *a):
        return False


class TestPullImageExistsPreCheck:
    """PULL_IMAGE 前置探测：镜像已在节点本地 → 记 SUCCESS 短路，不建 PULL task 不触发 pull。"""

    async def test_image_present_records_success_task(self, monkeypatch):
        """image_exists=True → 直接建 SUCCESS task（无 PULL task、无下发），并推进 START_HEAD。"""
        ctx, gotos, dispatched = _install_pull_step(monkeypatch, probe=True)
        await reconciler._step_pull_image("dep-test-1234")

        # 直接记 SUCCESS task（未走 ensure_task_row 下发 PULL）+ 补「跳过拉取」日志行
        assert len(ctx.session.added) == 2
        assert ctx.session.added[0].status == "SUCCESS"
        assert ctx.session.added[0].type == "PULL_IMAGE"
        assert ctx.session.added[0].resource_key.startswith("image:node-a:")  # 按 node+镜像 hash 键控
        assert isinstance(ctx.session.added[1], DeployLog)
        assert ctx.session.added[1].message == "镜像已存在，跳过拉取"
        assert dispatched == []  # 未下发任何 pull
        assert gotos == ["START_HEAD"]  # 全部 SUCCESS → 推进

    async def test_image_missing_creates_pull_task(self, monkeypatch):
        """image_exists=False → 走原 PULL task 流程（ensure_task_row 建 PENDING + 下发）。"""
        ctx, gotos, dispatched = _install_pull_step(monkeypatch, probe=False)
        await reconciler._step_pull_image("dep-test-1234")
        assert dispatched  # 有 PULL task 被下发
        assert ctx.session.added[0].status == "PENDING"
        assert gotos == []  # 新 PENDING 行对 all-SUCCESS 门可见 → 等终态再推进

    async def test_stale_success_row_repulls(self, monkeypatch):
        """历史 SUCCESS 行但镜像已不在节点（rmi/prune 或假成功年代）→ 不得盲信短路，
        必须重建 attempt 重拉——否则每次部署都「跳过拉取 → 启动 No such image」。"""
        ctx, gotos, dispatched = _install_pull_step(
            monkeypatch, probe=False, extra_rows=[_stale_success_task()], new_attempt=2
        )
        await reconciler._step_pull_image("dep-test-1234")

        # 重建 PULL task 下发重拉 + 时间线记「复核不在位」行；新 PENDING 行对
        # all-SUCCESS 门可见 → 本轮不推进阶段（生产语义：等重拉终态再收敛）
        assert dispatched and dispatched[0].id == "t-new-2"
        assert gotos == []
        logs = [a for a in ctx.session.added if isinstance(a, DeployLog)]
        assert len(logs) == 1
        assert "镜像复核不在位" in logs[0].message
        assert not any(a.message == "镜像已存在，跳过拉取" for a in logs)

    async def test_stale_success_probe_error_keeps_shortcut(self, monkeypatch):
        """SUCCESS 复核通道异常（节点暂时抖动/RPC 失败）→ 保守信任短路，不触发重拉。"""
        ctx, gotos, dispatched = _install_pull_step(monkeypatch, probe=None, extra_rows=[_stale_success_task()])
        await reconciler._step_pull_image("dep-test-1234")

        # 探测通道异常 → 信任 SUCCESS 短路，直接推进，无重拉
        assert dispatched == []
        assert gotos == ["START_HEAD"]


class _AddSession:
    def __init__(self):
        self.added = []

    async def get(self, cls, pk):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _AddCtx:
    def __init__(self):
        self.session = _AddSession()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def _stale_success_task() -> Task:
    """历史假成功/已被 prune 的 SUCCESS 行（resource_key 与 node-a/img:1 对齐）。"""
    return Task(
        id="t-old",
        resource_type="IMAGE_PULL",
        resource_key=pull_resource_key("node-a", "img:1"),
        node_id="node-a",
        type="PULL_IMAGE",
        status="SUCCESS",
        payload={"image": "img:1"},
        attempt=1,
    )


def _install_pull_step(
    monkeypatch, *, probe: bool | None, extra_rows: list[Task] | None = None, new_attempt: int = 1
) -> tuple[_AddCtx, list[str], list]:
    """给 ``_step_pull_image`` 装全套桩，返回 (ctx, gotos, dispatched)。

    ``probe`` 是 ``dispatch.image_exists`` 的返回（None = 探测通道抛异常）；
    ``extra_rows`` 是预置 task 行（stale SUCCESS 等）；``new_attempt`` 是桩建的
    PENDING task 的 attempt（重拉场景 = 2）。
    """
    dep = _dep("PULL_IMAGE")
    inst = Instance(
        instance_id="inst-1",
        deployment_id="dep-test-1234",
        role="full",
        rank=0,
        assigned_node="node-a",
        status="PENDING",
        spec={"instance_id": "inst-1", "rank": 0, "node_rank": 0, "gpu_arch": "nvidia", "model_path": "/m"},
    )
    cfg = reconciler.DeploymentConfig(image_tag="0.27.1")
    ctx = _AddCtx()
    gotos: list[str] = []
    dispatched: list = []
    preset = list(extra_rows or [])

    async def _load(deployment_id):
        return dep, None, cfg, [inst]

    async def _task_rows(keys):
        # 与生产同口径：预置行 + 本事务新行（ensure 建的行对后续查询可见）
        rows = preset + [t for t in ctx.session.added if isinstance(t, Task)]
        return [t for t in rows if t.resource_key in keys]

    async def _ensure_task_row(db, **kw):
        t = Task(
            id=f"t-new-{new_attempt}",
            resource_type=kw["resource_type"],
            resource_key=kw["resource_key"],
            node_id=kw["node_id"],
            type=kw["task_type"],
            status="PENDING",
            payload=kw["payload"],
            attempt=new_attempt,
        )
        db.add(t)
        return t, True, None

    async def _goto(deployment_id, phase):
        gotos.append(phase)

    async def _dispatch(t):
        dispatched.append(t)

    async def _probe(*a, **k):
        if probe is None:
            raise RuntimeError("node rpc down")
        return probe

    monkeypatch.setattr(reconciler, "get_db_context", lambda: ctx)
    monkeypatch.setattr(reconciler, "_load_context", _load)
    monkeypatch.setattr(reconciler, "resolve_image", lambda *a, **k: "img:1")
    monkeypatch.setattr(reconciler, "_task_rows_by_keys", _task_rows)
    monkeypatch.setattr(reconciler, "_goto_phase", _goto)
    monkeypatch.setattr(reconciler.dispatch, "image_exists", _probe)
    monkeypatch.setattr(reconciler.task_ops, "ensure_task_row", _ensure_task_row)
    monkeypatch.setattr(reconciler.task_ops, "dispatch_task", _dispatch)
    return ctx, gotos, dispatched
