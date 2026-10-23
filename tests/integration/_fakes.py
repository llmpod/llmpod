"""集成测试共享 FakeSession：按 PK 索引的内存 async session。

支持 select(Entity) / select(Entity).where(== | in_)（含 ``.with_for_update()`` 被忽略），
``get`` / ``add`` / ``flush`` / ``commit`` / ``rollback`` / ``delete``。属性写直接反映到存储
实例（同 ref），契合 service 层「读出行→改属性→flush」的写法。

另支持「字面量 update()」：``update(Entity).values(col=字面量)`` + 简单等值 where（如
reset_frozen_state 的归零）。仅覆盖字面量赋值与等值过滤，不支持表达式（balance+delta）、
func 聚合、RETURNING——需要这些的 service 请用 select+Python 或真实 PG 测。

限制：不支持复合 where、func 聚合、带表达式的 bulk update()/delete()。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.functions import Function

#: api_key 的三态可空列（未传=不改 / 显式 null=清空 / 传值=覆写）：service 与两侧路由测试
#: 共用同一份名单，避免新增可空列时三处字面量各自漂移。
CLEARABLE_KEY_FIELDS = ("rpm_limit", "tpm_limit", "total_token_quota", "budget_limit", "expires_at")

#: route（发布路由）的三态可空列：同上，service 与路由测试共用一份。
CLEARABLE_ROUTE_FIELDS = (
    "context_length",
    "input_price_per_mtok",
    "output_price_per_mtok",
    "cached_input_price_per_mtok",
)

#: route 的三态字段全集（不传=传哨兵「不改」）；显式 null 的结果各列不同——可空列落 NULL，
#: 非空的 failover_max_retries 落 0（见 service 写路径），测试侧的期望值见 _TRI_STATE_CLEARED。
TRI_STATE_ROUTE_FIELDS = CLEARABLE_ROUTE_FIELDS + ("failover_max_retries",)

#: API Key 配额/预算列的物理上界用例：(字段, 列能真正存下的最大值)。admin 与 tenant 两端
#: 共用一份，同 CLEARABLE_KEY_FIELDS 的用意。故意写字面量而非引用 schemas/request.py 的常量：
#: 这里锁的是「上界 = 列容量」，引用常量会退化成同义反复，列被加宽后也照样绿。
KEY_QUOTA_MAX_CASES = (
    ("rpm_limit", 2**31 - 1),
    ("tpm_limit", 2**31 - 1),
    ("total_token_quota", 2**63 - 1),  # 唯一的 BigInteger 列，上界与上面两个不同
    ("budget_limit", "99999999999999.999999"),
)

#: 上述字段的越界值：超出四个列容量中最小者（Numeric(20,6) ≈ 1e14）十四个数量级，
#: 任何上界都必须拦住它——从前它直达 asyncpg 参数绑定 → 裸 500。wallet 调额用例同用此值。
NUMERIC_OVERSIZED = 10**28
KEY_QUOTA_FIELDS = tuple(field for field, _ in KEY_QUOTA_MAX_CASES)

#: user.phone / user.email 的列宽用例：(字段, 列能真正存下的最大长度)。admin 与 tenant
#: 两侧共用一份，同 CLEARABLE_KEY_FIELDS 的用意。admin_ 前缀那份是建租户时引导的超管字段，
#: 落到同一个 user 表，故列宽相同。
PROFILE_MAX_CASES = (("phone", 32), ("email", 128))
TENANT_ADMIN_PROFILE_MAX_CASES = tuple((f"admin_{field}", n) for field, n in PROFILE_MAX_CASES)


def _flatten_conds(wc):
    """AND 链扁平化为单条件列表（BooleanClauseList.cycles 递归展开）。"""
    clauses = getattr(wc, "clauses", None)
    if clauses is not None:
        out: list = []
        for c in clauses:
            out.extend(_flatten_conds(c))
        return out
    return [wc] if wc is not None else []


class FakeSession:
    def __init__(self):
        self._store: dict = {}  # type -> {pk: obj}
        self._staged: list = []

    def preload(self, obj):
        self._store.setdefault(type(obj), {})[self._pk(obj)] = obj
        return obj

    def all(self, cls):
        return list(self._store.get(cls, {}).values())

    def _pk(self, obj):
        # 复合主键（如 node_model 的 (model_id, node_id)）返回 tuple 键，
        # 防同首列不同次列的行互相覆盖；单主键保持标量键（兼容既有 get(cls, pk) 调用）。
        pk_cols = list(obj.__table__.primary_key.columns)
        if len(pk_cols) > 1:
            return tuple(getattr(obj, c.key, None) for c in pk_cols)
        if pk_cols:
            return getattr(obj, pk_cols[0].key, None)
        return None

    async def get(self, cls, pk, *, populate_existing: bool = False):
        # populate_existing：真实 session 重读库覆盖 identity-map 旧值；桩的 store 即
        # 「库」，直接返回同一对象语义等价（桩内没有并发改库这回事）
        return self._store.get(cls, {}).get(pk)

    def add(self, obj):
        self._staged.append(obj)

    def begin_nested(self):
        """SAVEPOINT 桩：service 用嵌套事务隔离局部落库失败，桩内还原为直通上下文
        （失败回滚到保存点的语义由 service 的 try/except 承担，桩不模拟）。"""

        @asynccontextmanager
        async def _sp():
            yield

        return _sp()

    async def flush(self):
        for obj in self._staged:
            cls = type(obj)
            pk = self._pk(obj)
            if pk is None and hasattr(obj, "id"):
                # 自增从该表现有最大整型主键之后起（防与 preload 的固定 id 撞键）
                existing = self._store.get(cls, {})
                n = max((k for k in existing if isinstance(k, int)), default=0) + 1
                obj.id = n
                pk = n
            self._store.setdefault(cls, {})[pk] = obj
        self._staged = []

    async def commit(self):
        pass

    async def rollback(self):
        self._staged = []

    async def refresh(self, *_a, **_k):
        pass

    async def execute(self, stmt):
        if isinstance(stmt, Update):
            return self._execute_update(stmt)
        if isinstance(stmt, Delete):
            return self._execute_delete(stmt)
        if not hasattr(stmt, "column_descriptions"):
            return _FakeResult([])
        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        if entity is None:
            # select(func.count()).select_from(X)：entity 在 froms 里，按表名回查 cls
            for f in getattr(stmt, "get_final_froms", lambda: [])() or []:
                entity = self._cls_for_table(getattr(f, "name", ""))
                if entity is not None:
                    break
        objs = list(self._store.get(entity, {}).values())
        # where 过滤：AND 链扁平化为单条件，逐条等值/IN。**不支持 OR**——摊平后
        # ``or_(A, B)`` 会被当 AND 逐条求值（不是抛错，是静默收窄），带 OR 的查询
        # 别用 FakeSession 验结果，改走 MiniDB（tests/unit/_minidb.py 支持 AND/OR 树）。
        for cond in _flatten_conds(stmt.whereclause):
            if not hasattr(cond, "left") or not hasattr(cond.left, "key"):
                continue
            right = cond.right
            val = getattr(right, "effective_value", None)
            if val is None:
                val = getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                objs = [o for o in objs if getattr(o, cond.left.key, None) in val]  # IN
            else:
                objs = [o for o in objs if getattr(o, cond.left.key, None) == val]  # 等值
        # order_by：取一元 order 表达式的列名（负号=UnaryExpression 包 Column），按其排序
        # （desc 是带 desc 函数的 UnaryExpression；asc 裸 Column 直接命中）
        for ob in reversed(getattr(stmt, "_order_by_clauses", None) or []):
            col = getattr(ob, "element", ob)
            name = getattr(col, "key", None)
            if not name:
                continue
            objs = sorted(
                objs,
                key=lambda o: (getattr(o, name, None) is None, getattr(o, name, None)),
                reverse=hasattr(ob, "element") and getattr(ob, "modifier", None) is not None,
            )
        # offset + limit：切片（BindParameter，value 携带条数；SQL 语义先 offset 后 limit）
        if getattr(stmt, "_offset_clause", None) is not None:
            objs = objs[int(stmt._offset_clause.value or 0) :]
        if getattr(stmt, "_limit_clause", None) is not None:
            objs = objs[: int(stmt._limit_clause.value or 0)]
        # 聚合列（func.count()：column_descriptions[0].expr 是 Function）→ 返回行数
        descs = stmt.column_descriptions or []
        if descs and isinstance(descs[0].get("expr"), Function):
            return _FakeResult([len(objs)])
        # 单列投影 select(Entity.col)（expr 是 Column 而非实体本身）→ 该列标量流，
        # 如封禁集查询 select(TenantModelBlock.route_id)
        if len(descs) == 1 and descs[0].get("expr") is not entity:
            name = descs[0]["name"]
            return _FakeResult([getattr(o, name, None) for o in objs])
        # 多列 select → tuple 行（.all() 消费方按位置解包，如 select(Cluster.id, Cluster.name)）
        if len(descs) >= 2:
            names = [d["name"] for d in descs]
            return _FakeResult([tuple(getattr(o, n, None) for n in names) for o in objs])
        return _FakeResult(objs)

    def _cls_for_table(self, table_name: str):
        """按表名反查已 preload 的 ORM 类（count 聚合的 entity 解析用）。"""
        for c in self._store:
            t = getattr(c, "__table__", None)
            if t is not None and t.name == table_name:
                return c
        return None

    def _execute_delete(self, stmt):
        """字面量 delete()：等值/IN where 匹配的行从 store 移除（级联删 GPU 等用）。"""
        table = stmt.table
        cls = None
        for c in self._store:
            t = getattr(c, "__table__", None)
            if t is not None and t.name == table.name:
                cls = c
                break
        if cls is None:
            return _FakeResult([])
        store = self._store.get(cls, {})
        wc = stmt.whereclause
        doomed: list = []
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None)
            if val is None:
                val = getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                doomed = [pk for pk, o in store.items() if getattr(o, wc.left.key, None) in val]
            else:
                doomed = [pk for pk, o in store.items() if getattr(o, wc.left.key, None) == val]
        else:
            doomed = list(store.keys())
        for pk in doomed:
            store.pop(pk, None)
        return _FakeResult([])

    def _execute_update(self, stmt):
        """字面量 update()：仅支持等值 where + 字面量赋值（无表达式/聚合/RETURNING）。"""
        table = stmt.table
        cls = None
        for c in self._store:
            t = getattr(c, "__table__", None)
            if t is not None and t.name == table.name:
                cls = c
                break
        if cls is None:
            return _FakeResult([])
        objs = list(self._store.get(cls, {}).values())
        wc = stmt.whereclause
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None)
            if val is None:
                val = getattr(right, "value", right)
            objs = [o for o in objs if getattr(o, wc.left.key, None) == val]
        values = getattr(stmt, "_values", None) or {}
        for obj in objs:
            for col, val in values.items():
                key = col.key if hasattr(col, "key") else col
                val = getattr(val, "value", val)
                setattr(obj, key, val)
        return _FakeResult(objs)

    async def delete(self, obj):
        for d in self._store.values():
            for k in list(d):
                if d[k] is obj:
                    del d[k]


class _FakeResult:
    def __init__(self, objs):
        self._objs = list(objs)

    def all(self):
        return self._objs

    def scalars(self):
        return self._objs

    def scalar(self):
        first = self._objs[0] if self._objs else None
        return first

    def scalar_one_or_none(self):
        # 对齐真实 SQLAlchemy：多行抛 MultipleResultsFound——被测查询缺唯一约束时
        # 直接红，而不是静默取首行掩盖冲突（与 tests/unit/_minidb 同口径）
        if len(self._objs) > 1:
            raise MultipleResultsFound(
                f"FakeResult.scalar_one_or_none got {len(self._objs)} rows — 被测查询缺 limit/唯一约束"
            )
        return self._objs[0] if self._objs else None


def stub_model_verify(monkeypatch, session, model_id, node_ids):
    """staging 复核桩：预置终态 ``SUCCESS`` 的 ENSURE_MODEL 任务 + ``ensure_present`` 返回其 id。

    ``_stage_models`` / 扩容 staging 只认复核任务结论放行（``node_model.status`` 只是上次
    上报的缓存），FakeSession 没有真实节点上报，故用桩任务模拟「复核通过」。
    """
    from llmpod.apps.manager.deployment.placement import PlacementService
    from llmpod.models.task import Task

    task_ids: dict[str, str] = {}
    for node_id in node_ids:
        task = session.preload(
            Task(
                id=f"verify-{model_id}-{node_id}",
                resource_type="MODEL_PLACEMENT",
                resource_key=f"model:{model_id}:{node_id}",
                node_id=node_id,
                type="ENSURE_MODEL",
                status="SUCCESS",
                payload={},
                attempt=1,
            )
        )
        task_ids[node_id] = task.id

    async def _ensure(self, mid, nid):
        return task_ids.get(nid)

    monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
    return task_ids


def admin_session(**overrides):
    """集成测试通用超管会话（require_permission 见 is_superuser=True 直接短路，不查 DB）。

    覆盖例：``admin_session(role_ids=[])``。"""
    from llmpod.apps.webserver.auth.session import SessionData

    base = dict(
        user_id=1,
        username="tester",
        tenant_id=0,
        access_domain="platform",
        role_codes=["super_admin"],
        role_ids=[1],
        is_superuser=True,
    )
    base.update(overrides)
    return SessionData(**base)


def tenant_session(**overrides):
    """集成测试通用租户域会话（tenant_id=5；require_tenant_access 外层门禁按域放行）。

    覆盖例：``tenant_session(tenant_id=7)``。
    """
    from llmpod.apps.webserver.auth.session import SessionData

    base = dict(
        user_id=9,
        username="tenant-user",
        tenant_id=5,
        access_domain="tenant",
        role_codes=["tenant_admin"],
        role_ids=[11],
        is_superuser=True,
    )
    base.update(overrides)
    return SessionData(**base)


class _FakePipe:
    """非事务 pipeline：缓冲命令、execute 一次刷（FakeRedis 语义子集）。"""

    def __init__(self, r: "FakeRedis"):
        self._r = r
        self._ops: list[tuple] = []

    def set(self, key, value, ex=None):
        self._ops.append(("set", key, value, ex))
        return self

    def delete(self, *keys):
        self._ops.append(("delete", keys))
        return self

    async def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "set":
                await self._r.set(op[1], op[2], ex=op[3])
                out.append(True)
            elif op[0] == "delete":
                # 真 pipeline 返回删除条数；FakeRedis.delete 无返回值，按请求数对齐
                out.append(len(op[1]))
                await self._r.delete(*op[1])
            else:
                # 不支持的命令直接红，防桩静默吞掉写命令掩盖生产行为差异
                raise NotImplementedError(f"_FakePipe 不支持的 pipeline 命令: {op[0]}")
        self._ops = []
        return out


class FakeRedis:
    """网关管道可用的内存 Redis 桩（鉴权缓存 / 限流 / 配额计数用的全套方法）。

    比 test_api_key_service.FakeRedis 多 incr/incrby/expire（proxyserver 的
    RateLimiter / QuotaCounters 需要）；多个 proxy 集成测试文件共用，勿再复制。
    """

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = str(value)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def pipeline(self, transaction: bool = True):
        return _FakePipe(self)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def incrby(self, key, amount):
        self.store[key] = str(int(self.store.get(key, "0")) + amount)
        return int(self.store[key])

    async def expire(self, key, seconds):
        return True
