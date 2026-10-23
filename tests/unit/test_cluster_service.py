"""cluster_service.delete_cluster 单元测试（fake session，无真实 DB）。

验证三条分支：无节点→删除成功、有节点→Conflict、集群不存在→False。
"""

from __future__ import annotations

import pytest

from llmpod.core.exceptions import Conflict
from llmpod.models.cluster import Cluster
from llmpod.services.cluster_service import delete_cluster


class _FakeResult:
    def __init__(self, *, one=None, scalar=None):
        self._one = one
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._one

    def scalar(self):
        return self._scalar


class _FakeSession:
    """按调用顺序消费预设结果的 async session 桩。"""

    def __init__(self, results):
        self._results = list(results)
        self.deleted = []
        self.flushed = 0

    async def execute(self, stmt):
        return self._results.pop(0) if self._results else _FakeResult()

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flushed += 1


def _cluster(cid: int = 1) -> Cluster:
    return Cluster(id=cid, name="C", type="docker", description="", status="active", spec={})


class TestDeleteCluster:
    async def test_delete_when_no_nodes(self):
        db = _FakeSession([_FakeResult(one=_cluster()), _FakeResult(scalar=0)])
        ok = await delete_cluster(db, 1)
        assert ok is True
        assert db.deleted and db.deleted[0].id == 1
        assert db.flushed == 1

    async def test_refuse_when_has_nodes(self):
        db = _FakeSession([_FakeResult(one=_cluster()), _FakeResult(scalar=3)])
        with pytest.raises(Conflict):
            await delete_cluster(db, 1)
        assert db.deleted == []

    async def test_not_found_returns_false(self):
        db = _FakeSession([_FakeResult(one=None)])
        ok = await delete_cluster(db, 99)
        assert ok is False
        assert db.deleted == []
