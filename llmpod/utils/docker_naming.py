"""Docker 容器命名规范。"""

from __future__ import annotations

LLMPOD_CONTAINER_PREFIX = "llmpod"

# 容器 label keys：producer（orchestrator 的 build_run_spec）与 cleanup label-filter
# （orchestrator cleanup_deployment_containers）共享唯一定义处，避免一侧改名另一侧静默失配。
LABEL_MANAGED = f"{LLMPOD_CONTAINER_PREFIX}.managed"
LABEL_DEPLOYMENT_ID = f"{LLMPOD_CONTAINER_PREFIX}.deployment-id"
LABEL_INSTANCE_ID = f"{LLMPOD_CONTAINER_PREFIX}.instance-id"
LABEL_NODE_ID = f"{LLMPOD_CONTAINER_PREFIX}.node-id"
LABEL_ROLE = f"{LLMPOD_CONTAINER_PREFIX}.role"


def container_name(cluster_id: int, node_id: str, service: str) -> str:
    """生成一致的容器名称。

    格式：llmpod-{cluster_id}-{node_short}-{service}
    """
    node_short = node_id[:8]
    return f"{LLMPOD_CONTAINER_PREFIX}-{cluster_id}-{node_short}-{service}"


def deployment_container_name(deployment_id: str, instance_id: str) -> str:
    """生成推理部署容器名称（deployment_id 取前 8 字符防 birthday 碰撞）。

    格式：llmpod-{deployment_id[:8]}-{instance_id}
    完整 deployment_id/instance_id 进 labels 供 docker_list 反查。
    """
    return f"{LLMPOD_CONTAINER_PREFIX}-{deployment_id[:8]}-{instance_id}"
