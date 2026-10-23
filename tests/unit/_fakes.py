"""unit 层共享桩：语句文本分发的 FakeDB + FakeResult + MetricRollup 行替身。

FakeDB 按 ``FROM <表名>`` 分发 canned 行；不认识的语句直接抛错（防桩悄悄
漏喂导致恒真断言）。与 integration/_fakes.FakeSession 互补：本桩面向带 func 聚合 /
多列 select 的 service 单测（FakeSession 不支持这些形状）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from llmpod.services.metric_query_service import LATENCY_BUCKET_BOUNDS

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai 固定偏移，测试环境无 tzdata 依赖


def rollup_row(**kw):
    """MetricRollup 替身（merge_bucket_rows / trend / summarize 只读这些属性）。"""
    base = SimpleNamespace(
        requests=0,
        status="success",
        model_alias=None,
        tenant_id=None,
        total_tokens=0,
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        retry_count=0,
        stream_requests=0,
        latency_sum_ms=0,
        latency_count=0,
        ttft_sum_ms=0,
        ttft_count=0,
        in_flight_peak=0,
        latency_buckets=[0] * (len(LATENCY_BUCKET_BOUNDS) + 1),
        ttft_buckets=[0] * (len(LATENCY_BUCKET_BOUNDS) + 1),
        bucket_start=datetime(2026, 9, 5, 10, 0, tzinfo=TZ),
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


class FakeResult:
    """execute 结果替身：all/scalars 返回同一批行，scalar/…_one_or_none 取首个。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def scalars(self):
        return self._rows

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """按语句文本特征分发的最小 async session。

    ``tables: {表名: 行列表 | 可调用}``；可调用接收语句文本并返回行列表，用于
    同一张表被多条不同形状语句查询时按文本特征分发（如 GROUP BY 聚合 vs 多列
    投影），不认识的形状应在可调用内抛错。表名匹配要求边界（防 ``node`` 前缀
    误命中 ``node_model``）。
    """

    def __init__(self, tables: dict[str, list]):
        self._tables = tables
        self.statements: list[str] = []

    async def execute(self, stmt):
        text = str(stmt)
        self.statements.append(text)
        for table, rows in self._tables.items():
            if re.search(rf"FROM {re.escape(table)}(?![A-Za-z0-9_])", text):
                resolved = rows(text) if callable(rows) else rows
                return FakeResult(resolved)
        raise AssertionError(f"unexpected statement: {text[:120]}")


class FixedClock:
    """可推进的单调时钟桩（selector / session_affinity 单测注入用）。"""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def route_target(endpoint_id, priority=0, weight=1):
    """RouteTarget 替身（端点选择器 / 亲和单测共用；按内容直命名，避免各文件各造）。"""
    from llmpod.apps.proxyserver.snapshot import RouteTarget

    return RouteTarget(
        endpoint_id=endpoint_id,
        deployment_id=f"dep-{endpoint_id}",
        api_base=f"http://10.0.0.{endpoint_id}:8000",
        path="/v1/chat/completions",
        served_model="m",
        engine_type="vllm",
        weight=weight,
        priority=priority,
    )


class FakeSession:
    """按实体分桶的最小 async session（inspect 系直调用例共用）。

    支持 ``select(Entity).where(col == v | col.in_(vs))`` 的 ``scalars()`` 取行；带聚合 /
    join / 表达式条件的语句不认识（返回桶内全量或抛 AttributeError，调用方自行 stub）。
    与 integration/_fakes.FakeSession 同构，unit 侧独立一份避免跨层 import。
    """

    def __init__(self):
        self._store: dict = {}
        self._staged: list = []
        self._counters: dict = {}

    def preload(self, obj):
        self._store.setdefault(type(obj), {})[self._pk(obj)] = obj
        return obj

    def _pk(self, obj):
        pk = list(obj.__table__.primary_key.columns)
        return getattr(obj, pk[0].key, None) if pk else None

    async def get(self, cls, pk):
        return self._store.get(cls, {}).get(pk)

    def add(self, obj):
        self._staged.append(obj)

    async def flush(self):
        for obj in self._staged:
            cls = type(obj)
            pk = self._pk(obj)
            if pk is None and hasattr(obj, "id"):
                n = self._counters.get(cls, 0) + 1
                self._counters[cls] = n
                obj.id = n
                pk = n
            self._store.setdefault(cls, {})[pk] = obj
        self._staged = []

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, *_a, **_k):
        pass

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        objs = list(self._store.get(entity, {}).values())
        for cond in _where_conditions(stmt.whereclause):
            objs = [o for o in objs if cond(o)]
        return _SessionResult(objs)

    async def delete(self, obj):
        for d in self._store.values():
            for k in [k for k, v in d.items() if v is obj]:
                del d[k]


def _where_conditions(whereclause):
    """把 ``where(a == v, b.in_(vs))`` 的条件树摊平成一组对象谓词。

    支持 and_ 链 / == / in_ 三种形状（inspect 系直调用例只用到这些）；其它表达式形状
    不认识，直接忽略条件（调用方若需要更强语义应在种子数据上避免该形状）。
    """

    def _bind_value(right):
        return getattr(right, "effective_value", None) or getattr(right, "value", right)

    def _binary(cond):
        key = cond.left.key
        val = _bind_value(cond.right)
        if isinstance(val, (list, tuple, set)):
            return lambda o: getattr(o, key, None) in val
        return lambda o: getattr(o, key, None) == val

    def _walk(node, out):
        clauses = getattr(node, "clauses", None)
        if clauses is not None and not hasattr(node, "left"):
            for c in clauses:
                _walk(c, out)
            return
        if hasattr(node, "left") and hasattr(node.left, "key"):
            out.append(_binary(node))

    out: list = []
    if whereclause is not None:
        _walk(whereclause, out)
    return out


class _SessionResult:
    def __init__(self, objs):
        self._objs = list(objs)

    def scalars(self):
        return self._objs

    def scalar_one_or_none(self):
        return self._objs[0] if self._objs else None
