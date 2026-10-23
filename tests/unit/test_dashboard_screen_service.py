"""dashboard_screen_service 聚合单测：直调真实 screen()（集成测试只 monkeypatch
本 service，聚合本体没有 CI 覆盖）。

语句形状（GROUP BY 聚合 / 多列投影）超出 FakeSession 支持，用 unit/_fakes.FakeDB
的文本分发喂 canned 行；实时 GPU 缓冲经 monkeypatch node_gpu_summaries 注入
（缓冲属 Manager 进程内状态，单测不可达）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from tests.unit._fakes import TZ, FakeDB, rollup_row

from llmpod.apps.manager.deployment.constants import DeploymentStatus, DeployStep, LogLevel
from llmpod.apps.webserver.routes.admin.dashboard import ScreenOut
from llmpod.config.constants import NodeStatus
from llmpod.services import dashboard_screen_service as svc
from llmpod.services.dashboard_screen_service import screen


def _node_rows(text):
    """node 表两种形状：按集群 GROUP BY 聚合行 / (id, cluster_id, status) 成员行。"""
    if "GROUP BY" in text:
        # (cluster_id, 节点总数, 在线数)
        return [(1, 2, 1), (2, 1, 1)]
    if "node.id, node.cluster_id, node.status" in text:
        # 有 GPU 节点的成员行：cluster 1 的 n2 离线，n1/n3 在线
        return [
            ("n1", 1, NodeStatus.ONLINE),
            ("n2", 1, NodeStatus.OFFLINE),
            ("n3", 2, NodeStatus.ONLINE),
        ]
    raise AssertionError(f"unexpected node stmt: {text[:120]}")


# gpu 只有一种形状：按节点 GROUP BY 聚合行 (node_id, 卡数, 在线卡数)
_GPU_ROWS = [("n1", 3, 3), ("n2", 2, 2), ("n3", 1, 1)]


def _deploy_rows(text):
    """model_deployment 三种形状：状态 GROUP BY / 名称回查 IN / 最近部署三列。"""
    if "GROUP BY" in text:
        return [(DeploymentStatus.RUNNING, 2), (DeploymentStatus.FAILED, 1)]
    if " IN (" in text:
        return [("dep-1", "模型A")]
    if "ORDER BY" in text:
        return [
            ("dep-1", "模型A", DeploymentStatus.RUNNING),
            ("dep-2", "模型B", DeploymentStatus.FAILED),
        ]
    raise AssertionError(f"unexpected model_deployment stmt: {text[:120]}")


def _make_db(*, rollup_rows=(), audit_rows=(), deploy_log_rows=()):
    return FakeDB(
        {
            "metric_rollup": rollup_rows,
            "node": _node_rows,
            "gpu": _GPU_ROWS,
            "cluster": [SimpleNamespace(id=1, name="算力一区"), SimpleNamespace(id=2, name="算力二区")],
            "model_deployment": _deploy_rows,
            # (actor_id, actor_name, action, resource_type, resource_id, created_at)
            "audit_log": audit_rows,
            # 属性访问行（_deploy_events 按 r.<col> 取值）
            "deploy_log": deploy_log_rows,
        }
    )


class TestScreen:
    async def test_full_aggregate(self, monkeypatch):
        """整屏装配：指标卡 / 顶卡计数 / 集群池（离线成员不进实时加权）/ 部署 / 事件流。"""
        # 实时 GPU 摘要：n1 三卡 40% + n3 单卡 80%（n2 离线不进加权）
        monkeypatch.setattr(
            svc,
            "node_gpu_summaries",
            lambda ids: {
                "n1": {"gpu_count": 3, "gpu_utilization": 40.0, "used_vram_gb": 10.0, "total_vram_gb": 60.0},
                "n3": {"gpu_count": 1, "gpu_utilization": 80.0, "used_vram_gb": 30.0, "total_vram_gb": 20.0},
            },
        )
        t1 = datetime(2026, 9, 5, 14, 0, tzinfo=TZ)
        t2 = datetime(2026, 9, 5, 13, 0, tzinfo=TZ)
        db = _make_db(
            rollup_rows=[
                rollup_row(requests=100, model_alias="deepseek", tenant_id=1, ttft_sum_ms=3000, ttft_count=30),
                rollup_row(requests=2, status="error", model_alias="deepseek", tenant_id=1),
            ],
            audit_rows=[(7, "ops", "create", "cluster", "9", t1)],
            deploy_log_rows=[
                SimpleNamespace(
                    deployment_id="dep-1",
                    level=LogLevel.ERROR,
                    step=DeployStep.START,
                    message="启动失败",
                    created_at=t2,
                )
            ],
        )

        out = await screen(db)

        # 指标卡：触达上游 102、错误率 2/102、平均 TTFT 100ms
        assert out["calls"] == 102
        assert out["error_rate"] == round(2 / 102, 4)
        assert out["avg_ttft_ms"] == 100
        # 顶卡：节点 3/2（n2 离线）；GPU 6 全在线
        assert (out["total_nodes"], out["online_nodes"]) == (3, 2)
        assert (out["gpu_total"], out["gpu_online"]) == (6, 6)
        # 集群池：静态计数来自 GROUP BY，实时均值只对在线成员加权
        c1, c2 = out["clusters"]
        assert (c1["id"], c1["node_count"], c1["online_node_count"], c1["gpu_count"]) == (1, 2, 1, 5)
        assert (c1["gpu_utilization"], c1["used_vram_gb"], c1["total_vram_gb"]) == (40.0, 10.0, 60.0)
        assert (c2["node_count"], c2["online_node_count"], c2["gpu_count"]) == (1, 1, 1)
        assert (c2["gpu_utilization"], c2["total_vram_gb"]) == (80.0, 20.0)
        # 部署计数（未知状态不入表）+ 最近部署
        assert out["deployments"][DeploymentStatus.RUNNING] == 2
        assert out["deployments"][DeploymentStatus.FAILED] == 1
        assert out["deployments"][DeploymentStatus.STOPPED] == 0
        assert [d["id"] for d in out["recent_deployments"]] == ["dep-1", "dep-2"]
        # 事件流：双源 MERGE 按时间倒序
        assert [(e["type"], e["text"]) for e in out["events"]] == [
            ("success", "ops 创建了集群 9"),
            ("error", "[模型A] 启动失败"),
        ]
        # 响应模型契约：真实 screen 产物须能直接喂路由的 ScreenOut（enum key 部署计数）
        m = ScreenOut(**out)
        assert m.deployments.running == 2 and m.deployments.failed == 1

    async def test_empty_data_zero_fallbacks(self, monkeypatch):
        """无 rollup / 无节点 / 无集群 / 无事件 → 零值兜底不抛。"""
        monkeypatch.setattr(svc, "node_gpu_summaries", lambda ids: {})
        db = FakeDB(
            {
                "metric_rollup": [],
                "node": [],
                "gpu": [],
                "cluster": [],
                "model_deployment": [],
                "audit_log": [],
                "deploy_log": [],
            }
        )
        out = await screen(db)
        assert out["calls"] == 0 and out["avg_ttft_ms"] is None
        assert (out["total_nodes"], out["online_nodes"], out["gpu_total"], out["gpu_online"]) == (0, 0, 0, 0)
        assert out["clusters"] == []
        assert all(v == 0 for v in out["deployments"].values())
        assert out["events"] == [] and out["recent_deployments"] == []
