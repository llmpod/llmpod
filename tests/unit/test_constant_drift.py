"""常量枚举值与 DB CHECK 约束字面量的一致性守卫。

CHECK 字面量是存储层权威出处（models 层），枚举是应用层权威出处（config/constants.py、
apps/manager/deployment/constants.py）。两处任一侧加/改状态而另一侧没跟，首个生产
写入才会以 IntegrityError 爆出——本测试把漂移提前到 CI。主表（deployment
status/phase、instance status）经 ``check_values`` 直接从 ORM 的 CheckConstraint
反射提取，改 models 层即红；其余多表/特例用例仍手抄字面量做哨兵（镜像即检测点）。
"""

from llmpod.apps.manager.deployment.constants import (
    DeploymentPhase,
    DeploymentStatus,
    DeployStep,
    EngineStatus,
    LogLevel,
    LogStatus,
    NodeModelDesiredState,
    NodeModelState,
    ReplicaStatus,
)
from llmpod.config.constants import (
    ClusterStatus,
    EnsureModelStage,
    GpuStatus,
    InstanceStatus,
    NodeStatus,
    PullImageStage,
    TaskStatus,
)


import re

from sqlalchemy import CheckConstraint

from llmpod.models import (
    Instance as InstanceTable,
    ModelDeployment,
)


def check_values(table, name: str) -> set[str]:
    """从 ORM 表的 CheckConstraint 文本提取 IN (...) 字面量（直读存储层定义，非手抄）。"""
    for c in table.constraints:
        if isinstance(c, CheckConstraint) and c.name == name:
            m = re.search(r"IN \((.*?)\)", c.sqltext.text if hasattr(c.sqltext, "text") else str(c.sqltext))
            assert m, f"{name} 非 IN 形态: {c.sqltext}"
            return {v.strip("' \"") for v in m.group(1).split(",")}
    raise AssertionError(f"未找到 CheckConstraint: {name}")


def _values(enum_cls) -> set[str]:
    return {m.value for m in enum_cls}


def test_deployment_status_matches_check():
    # ck_model_deployment_status（launching 是 DB CHECK 外的历史兼容值）
    assert _values(DeploymentStatus) - {"launching"} == check_values(
        ModelDeployment.__table__, "ck_model_deployment_status"
    )


def test_deployment_phase_matches_check():
    # ck_model_deployment_phase
    assert _values(DeploymentPhase) == check_values(ModelDeployment.__table__, "ck_model_deployment_phase")


def test_instance_status_matches_check():
    # ck_instance_status
    assert _values(InstanceStatus) == check_values(InstanceTable.__table__, "ck_instance_status")


def test_replica_layer_status_matches_checks():
    # ck_model_deployment_replica_status / ck_model_deployment_instance_status（四层投影层）
    assert _values(ReplicaStatus) == {"provisioning", "serving", "down"}
    assert _values(EngineStatus) == {"provisioning", "starting", "ready", "down"}


def test_task_status_matches_check():
    # ck_task_status
    assert _values(TaskStatus) == {"PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELLED"}


def test_node_model_matches_checks():
    # ck_node_model_status / ck_node_model_desired_state
    assert _values(NodeModelState) == {"absent", "downloading", "verifying", "present", "failed"}
    assert _values(NodeModelDesiredState) == {"present", "absent"}


def test_node_status_matches_check():
    # ck_node_status
    assert _values(NodeStatus) == {"online", "offline", "maintenance", "busy", "drained"}


def test_gpu_status_matches_check():
    # ck_gpu_status
    assert _values(GpuStatus) == {"online", "stale", "offline"}


def test_cluster_status_matches_check():
    # ck_cluster_status
    assert _values(ClusterStatus) == {"active", "inactive"}


def test_deploy_log_level_status_match_checks():
    # ck_deploy_log_level / ck_deploy_log_status
    assert _values(LogLevel) == {"debug", "info", "warn", "error"}
    assert _values(LogStatus) == {"pending", "running", "success", "failed", "cancelled"}


def test_deploy_step_whitelist_stable():
    """step 无 DB CHECK（应用层白名单），此处锁快照：新增步骤须同步前端时间线语义。"""
    assert _values(DeployStep) == {
        "queued",
        "staging_model",
        "pull_image",
        "start_head",
        "head_health_check",
        "start_workers",
        "worker_health_check",
        "finalize",
        "running",
        "submit",
        "start",
        "stop",
        "stopping",
        "scale",
        "start_instance",
        "orchestration",
    }


def test_task_stage_vocabularies_stable():
    """子阶段词表无 DB CHECK（开放词表，按任务类型各自成族），锁快照防意外漂移：
    值是 wire 契约 + 前端渲染依据，改名/删除是破坏性变更；允许跨族重复（两族都有
    finished）。"""
    assert _values(EnsureModelStage) == {"downloading", "verifying", "finished"}
    assert _values(PullImageStage) == {"pulling", "finished"}
