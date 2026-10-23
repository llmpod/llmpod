"""cluster_service 其余函数单测（delete_cluster 见既有 test_cluster_service.py）。

CRUD/清单走 ``_minidb.MiniDB`` 真过滤语义；GROUP BY+FILTER 统计形状用
``tests/unit/_fakes.FakeDB`` canned 行（MiniDB 不支持聚合）。
"""

from __future__ import annotations


from llmpod.models.cluster import Cluster
from llmpod.services import cluster_service as svc
from tests.unit._fakes import FakeDB
from tests.unit._minidb import MiniDB


def _mkdb(*clusters) -> MiniDB:
    db = MiniDB()
    for c in clusters:
        db.preload(c)
    return db


def _cluster(cid, name=None, status="active", description="") -> Cluster:
    return Cluster(id=cid, name=name or f"c{cid}", type="docker", description=description, status=status, spec={})


class TestCreateCluster:
    async def test_creates_and_flushes_with_id(self):
        db = MiniDB()
        cluster = await svc.create_cluster(db, "gpu-pool", "docker", "描述")
        assert cluster.name == "gpu-pool"
        assert cluster.type == "docker" and cluster.description == "描述"
        assert cluster.id is not None  # flush 分配自增 id
        assert [c.name for c in db.rows_of(Cluster)] == ["gpu-pool"]

    async def test_description_defaults_empty(self):
        db = MiniDB()
        cluster = await svc.create_cluster(db, "x", "docker")
        assert cluster.description == ""


class TestListClusters:
    async def test_lists_all_with_total(self):
        db = _mkdb(_cluster(1), _cluster(2), _cluster(3))
        rows, total = await svc.list_clusters(db)
        assert total == 3
        assert [c.id for c in rows] == [3, 2, 1]  # id 倒序

    async def test_status_filter(self):
        db = _mkdb(_cluster(1, status="active"), _cluster(2, status="disabled"), _cluster(3, status="active"))
        rows, total = await svc.list_clusters(db, status="active")
        assert total == 2
        assert [c.id for c in rows] == [3, 1]

    async def test_pagination(self):
        db = _mkdb(*[_cluster(i) for i in range(1, 6)])
        rows, total = await svc.list_clusters(db, page=2, page_size=2)
        assert total == 5
        assert [c.id for c in rows] == [3, 2]  # 第 2 页（id 倒序）

    async def test_empty(self):
        rows, total = await svc.list_clusters(MiniDB())
        assert rows == [] and total == 0


class TestGetCluster:
    async def test_by_id_found(self):
        db = _mkdb(_cluster(1), _cluster(2))
        assert (await svc.get_cluster(db, 2)).name == "c2"

    async def test_by_id_missing(self):
        assert await svc.get_cluster(MiniDB(), 42) is None

    async def test_by_name_found(self):
        db = _mkdb(_cluster(1, name="default"), _cluster(2, name="other"))
        assert (await svc.get_cluster_by_name(db, "default")).id == 1

    async def test_by_name_missing(self):
        assert await svc.get_cluster_by_name(MiniDB(), "nope") is None


class TestUpdateCluster:
    async def test_updates_all_fields(self):
        db = _mkdb(_cluster(1))
        out = await svc.update_cluster(db, 1, name="renamed", description="new", status="disabled")
        assert (out.name, out.description, out.status) == ("renamed", "new", "disabled")

    async def test_partial_update_keeps_none_fields(self):
        db = _mkdb(_cluster(1, description="old"))
        out = await svc.update_cluster(db, 1, name="only-name")
        assert out.name == "only-name"
        assert out.description == "old"  # None = 不修改
        assert out.status == "active"

    async def test_missing_returns_none(self):
        assert await svc.update_cluster(MiniDB(), 9, name="x") is None


class TestGetClusterNameMap:
    async def test_empty_ids_short_circuits(self):
        assert await svc.get_cluster_name_map(MiniDB(), []) == {}

    async def test_id_to_name_mapping(self):
        db = _mkdb(_cluster(1, name="a"), _cluster(3, name="c"))
        assert await svc.get_cluster_name_map(db, [1, 3]) == {1: "a", 3: "c"}

    async def test_unknown_ids_absent(self):
        db = _mkdb(_cluster(1, name="a"))
        assert await svc.get_cluster_name_map(db, [1, 99]) == {1: "a"}


class TestGetClusterStatsMap:
    @staticmethod
    def _stats_db() -> FakeDB:
        """两条聚合语句（节点 GROUP BY / GPU join GROUP BY）按文本分发 canned 行。"""
        return FakeDB(
            {
                "node": lambda text: [(1, 3, 2), (2, 1, 0)] if "JOIN gpu" not in text else [(1, 4)],
            }
        )

    async def test_maps_node_and_gpu_counts(self):
        stats = await svc.get_cluster_stats_map(self._stats_db(), [1, 2, 3])
        assert stats[1] == {"node_count": 3, "online_node_count": 2, "gpu_count": 4}
        # 仅节点无 GPU → gpu_count 0；稀疏键（3 无节点无 GPU）不出现
        assert stats[2] == {"node_count": 1, "online_node_count": 0, "gpu_count": 0}
        assert 3 not in stats

    async def test_empty_ids_short_circuits(self):
        assert await svc.get_cluster_stats_map(MiniDB(), []) == {}


class TestEnrichClusters:
    async def test_backfills_stats_in_place(self, monkeypatch):
        db = _mkdb(_cluster(1), _cluster(2))

        async def _stub(d, ids):
            assert ids == [1, 2]
            return {
                1: {"node_count": 3, "online_node_count": 2, "gpu_count": 4},
                2: {"node_count": 0, "online_node_count": 0, "gpu_count": 0},
            }

        monkeypatch.setattr(svc, "get_cluster_stats_map", _stub)
        clusters = db.rows_of(Cluster)
        out = await svc.enrich_clusters(db, clusters)
        assert out is clusters
        assert (clusters[0].node_count, clusters[0].online_node_count, clusters[0].gpu_count) == (3, 2, 4)

    async def test_missing_stats_default_zero(self, monkeypatch):
        async def _stub(d, ids):
            return {}

        monkeypatch.setattr(svc, "get_cluster_stats_map", _stub)
        clusters = [_cluster(7)]
        await svc.enrich_clusters(db=_mkdb(*clusters), clusters=clusters)
        assert (clusters[0].node_count, clusters[0].online_node_count, clusters[0].gpu_count) == (0, 0, 0)

    async def test_empty_list_passthrough(self):
        clusters: list = []
        assert await svc.enrich_clusters(MiniDB(), clusters) is clusters
