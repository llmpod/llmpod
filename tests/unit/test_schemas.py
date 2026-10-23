"""共享 Pydantic schemas 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from llmpod.apps.webserver.schemas.response import (
    ResultBase,
    PaginatedResponse,
)
from llmpod.apps.webserver.schemas.request import PaginationParams
from llmpod.apps.webserver.schemas.out_models import (
    UserDetailOut,
    RoleDetailOut,
    NodeDetailOut,
    GpuDetailOut,
    ClusterDetailOut,
    TenantDetailOut,
)


class TestResultBase:
    def test_defaults(self):
        resp = ResultBase()
        assert resp.code == 200
        assert resp.message == "success"
        assert resp.data is None


class TestPaginationParams:
    def test_defaults(self):
        p = PaginationParams()
        assert p.page == 1
        assert p.page_size == 20
        assert p.offset == 0

    def test_offset_calculation(self):
        p = PaginationParams(page=3, page_size=10)
        assert p.offset == 20


class TestPaginatedResponse:
    def test_create(self):
        items = ["a", "b", "c"]
        resp = PaginatedResponse.result(items=items, total=10, page=1, page_size=3)
        # 顶层统一三段式 {code, message, data}
        assert resp["code"] == 200
        assert resp["message"] == "success"
        # 分页字段下沉到 data
        data = resp["data"]
        assert data["items"] == ["a", "b", "c"]
        assert data["total"] == 10
        assert data["page"] == 1
        assert data["page_size"] == 3
        assert data["total_pages"] == 4  # 10 / 3 = 3.33 → 4


class TestDetailOutSchemas:
    """富化 ``*DetailOut``：继承单表列（orm_schema 产物）+ 叠加关联字段。"""

    def test_user_detail_out_has_role_ids_and_inherits_columns(self):
        f = UserDetailOut.model_fields
        # 关联字段
        assert "role_ids" in f
        # 所属租户余额（关联装配）
        for k in ("tenant_balance", "tenant_frozen_balance", "tenant_available"):
            assert k in f
        # 继承自 User 的列
        assert "username" in f and "tenant_id" in f and "status" in f
        # password_hash 仍被 exclude，绝不外泄
        assert "password_hash" not in f

    def test_role_detail_out_has_permission_ids(self):
        assert "permission_ids" in RoleDetailOut.model_fields

    def test_node_detail_out_has_cluster_name(self):
        assert "cluster_name" in NodeDetailOut.model_fields

    def test_gpu_detail_out_has_host_fields(self):
        f = GpuDetailOut.model_fields
        for k in ("node_hostname", "cluster_id", "cluster_name"):
            assert k in f

    def test_cluster_detail_out_has_counts(self):
        f = ClusterDetailOut.model_fields
        for k in ("node_count", "online_node_count", "gpu_count"):
            assert k in f

    def test_tenant_detail_out_has_counts(self):
        f = TenantDetailOut.model_fields
        for k in ("user_count", "role_count", "balance", "frozen_balance", "available"):
            assert k in f

    def test_enrichment_defaults_when_unset(self):
        """未回填关联属性时，from_attributes 安全降级到默认（空列表），绝不 500。"""

        class _O:
            pass

        o = _O()
        o.id = 1
        o.tenant_id = 0
        o.username = "x"
        o.status = "active"
        o.email = None
        o.phone = None
        o.last_login_at = None
        o.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        o.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # 故意不设 role_ids
        m = UserDetailOut.model_validate(o, from_attributes=True)
        assert m.role_ids == []
        assert m.username == "x"

    def test_enrichment_reads_setattr_attr(self):
        """回填后的 role_ids（经 setattr 设到 ORM 实例上）能被 from_attributes 读出。"""

        class _O:
            pass

        o = _O()
        o.id = 1
        o.tenant_id = 0
        o.username = "x"
        o.status = "active"
        o.email = None
        o.phone = None
        o.last_login_at = None
        o.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        o.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        o.role_ids = [5, 7]  # 模拟 enrich 回填
        m = UserDetailOut.model_validate(o, from_attributes=True)
        assert m.role_ids == [5, 7]
