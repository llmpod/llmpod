"""user_service.assert_not_last_admin 单元测试（fake session，无真实 DB）。

验证删除用户前的"管理员兜底"不变式：
- 未持管理员角色 → 放行；
- 持管理员角色且他人仍持有 → 放行；
- 是某管理员角色最后一名持有者 → raise LLMPodError。
"""

from __future__ import annotations

import pytest

from llmpod.core.exceptions import LLMPodError
from llmpod.services.user_service import (
    assert_not_last_admin,
    enrich_users_role_ids,
    get_role_ids_map,
)


class _FakeResult:
    def __init__(self, *, all=None, scalar=None, one=None):
        self._all = all if all is not None else []
        self._scalar = scalar
        self._one = one

    def all(self):
        return self._all

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """按调用顺序消费预设结果的 async session 桩。"""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        return self._results.pop(0) if self._results else _FakeResult()


class TestAssertNotLastAdmin:
    async def test_no_admin_role_passes(self):
        # 第一条查询：用户持有的管理员角色 id 列表为空
        db = _FakeSession([_FakeResult(all=[])])
        await assert_not_last_admin(db, user_id=10)  # 不抛即通过

    async def test_others_hold_admin_role_passes(self):
        # 持有角色 1，且另有 2 名持有者
        db = _FakeSession([_FakeResult(all=[(1,)]), _FakeResult(scalar=2)])
        await assert_not_last_admin(db, user_id=10)

    async def test_last_admin_raises(self):
        # 持有角色 1，他人持有数 0，角色 code=tenant_admin
        db = _FakeSession(
            [
                _FakeResult(all=[(1,)]),
                _FakeResult(scalar=0),
                _FakeResult(one="tenant_admin"),
            ]
        )
        with pytest.raises(LLMPodError):
            await assert_not_last_admin(db, user_id=10)


class TestGetRoleIdsMap:
    """get_role_ids_map：一条 IN 查询批量回填 user→[role_id]。"""

    async def test_empty_input_returns_empty(self):
        assert await get_role_ids_map(_FakeSession([]), []) == {}

    async def test_maps_user_to_role_ids(self):
        # (user_id, role_id) 行集；注意 loader 内按 role_id 排序，这里桩已排好
        db = _FakeSession([_FakeResult(all=[(10, 1), (10, 2), (20, 3)])])
        mapping = await get_role_ids_map(db, [10, 20])
        assert mapping == {10: [1, 2], 20: [3]}

    async def test_user_with_no_rows_absent(self):
        # 结果里没有 user 30 → 该 key 不出现（enrich 端补 []）
        db = _FakeSession([_FakeResult(all=[(10, 1)])])
        mapping = await get_role_ids_map(db, [10, 30])
        assert mapping == {10: [1]}


class TestEnrichUsersRoleIds:
    """enrich_users_role_ids：把 role_ids 回填到 ORM 实例，缺失补 []。"""

    async def test_sets_role_ids_in_place(self):
        class _U:
            def __init__(self, uid):
                self.id = uid

        users = [_U(10), _U(20)]
        db = _FakeSession([_FakeResult(all=[(10, 1), (10, 2)])])
        out = await enrich_users_role_ids(db, users)
        assert out is users
        assert users[0].role_ids == [1, 2]
        assert users[1].role_ids == []  # 无关联 → 默认空

    async def test_empty_list_short_circuits(self):
        assert await enrich_users_role_ids(_FakeSession([]), []) == []
