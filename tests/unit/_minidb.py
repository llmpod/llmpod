"""unit 层共享：操作符感知的内存 async session（MiniDB）。

与 ``_fakes.FakeDB``（语句文本分发 canned 行，面向 func 聚合形状）互补：MiniDB 按
语句 AST 真过滤/投影，覆盖 seed / auth / 请求日志等需要真实过滤语义的 service 单测。

支持：
- where：eq / ne / gt / ge / lt / le / in_ / is_（AND / OR 树递归求值，None 无条件透传）；
- ORM join（含嵌套）：按「表名 → 行对象」命名空间做笛卡尔积 + onclause 过滤；
- 投影：实体行 / 多列 tuple / 单列标量 / func.count（含 ``select_from(subquery)`` 计数）；
- order_by（asc/desc 混排，None 安全）+ offset/limit + distinct；
- delete()（等值/IN where）；add/flush（自增 id）/commit/refresh/get(cls, pk)。

不支持即抛 AssertionError（防桩悄悄漏喂导致恒真断言），GROUP BY 亦然（聚合统计
形状请用 ``_fakes.FakeDB`` canned 行）。
"""

from __future__ import annotations

import operator as pyop

from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.elements import False_ as _SAFalse
from sqlalchemy.sql.elements import Null
from sqlalchemy.sql.elements import True_ as _SATrue
from sqlalchemy.sql.functions import Function
from sqlalchemy.sql.selectable import Join, Subquery


class _Rows:
    """可迭代行视图：同时支持 .all()（seed.py 有 ``.scalars().all()`` 链式用法）。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)


class MiniResult:
    """execute 结果替身：all/scalars 返回同一批行，scalar/…_one_or_none 取首个。

    单列标量投影的行以 1-tuple 存放（对齐真实 Row：``.all()`` 供 ``for (pid,) in …``
    解包），``scalars()``/``scalar()``/``scalar_one_or_none()`` 自动解包为裸值。
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    @staticmethod
    def _unwrap(row):
        return row[0] if isinstance(row, tuple) and len(row) == 1 else row

    def scalars(self):
        return _Rows(self._unwrap(r) for r in self._rows)

    def scalar(self):
        return self._unwrap(self._rows[0]) if self._rows else None

    def scalar_one_or_none(self):
        if len(self._rows) > 1:
            raise MultipleResultsFound(
                f"MiniResult.scalar_one_or_none got {len(self._rows)} rows — "
                "被测查询缺 limit/唯一约束（真实 SQLAlchemy 同样会抛）"
            )
        return self._unwrap(self._rows[0]) if self._rows else None


class MiniDB:
    """内存 async session：按 ORM 类存行，execute 走真实过滤/投影语义。"""

    def __init__(self):
        self._store: dict[type, list] = {}
        self._staged: list = []
        self._next_id = 1
        self.deleted: list = []
        self.commits = 0

    # ---- 写侧 ----

    def preload(self, *objs):
        """直接放入已持久化行（自带 id）。"""
        for o in objs:
            self._store.setdefault(type(o), []).append(o)
        return objs[0] if len(objs) == 1 else objs

    def add(self, obj):
        self._staged.append(obj)

    async def flush(self):
        for obj in self._staged:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            self._store.setdefault(type(obj), []).append(obj)
        self._staged = []

    async def commit(self):
        await self.flush()
        self.commits += 1

    async def refresh(self, *_a, **_k):
        pass

    async def get(self, cls, pk):
        for o in self._store.get(cls, []):
            if getattr(o, "id", None) == pk:
                return o
        return None

    async def delete(self, obj):
        rows = self._store.get(type(obj), [])
        self._store[type(obj)] = [r for r in rows if r is not obj]
        self.deleted.append(obj)

    def rows_of(self, cls):
        """测试断言用：某类的当前全部行。"""
        return list(self._store.get(cls, []))

    # ---- 读侧 ----

    async def execute(self, stmt):
        if isinstance(stmt, Delete):
            return self._execute_delete(stmt)
        if isinstance(stmt, Update):
            return self._execute_update(stmt)
        return self._execute_select(stmt)

    def _execute_update(self, stmt) -> MiniResult:
        """字面量 update()：按 where 语义匹配行并逐列赋字面量值（表达式/聚合不支持）。"""
        rows = self._objs_for_table(stmt.table.name)
        values = {col.key: getattr(val, "value", val) for col, val in dict(stmt._values).items()}
        matched = []
        for r in rows:
            if self._eval_where(stmt.whereclause, {stmt.table.name: r}):
                for key, val in values.items():
                    setattr(r, key, val)
                matched.append(r)
        return MiniResult(matched)

    # ---- 内部：语句求值 ----

    def _objs_for_table(self, table_name: str) -> list:
        for cls, rows in self._store.items():
            if getattr(cls, "__tablename__", None) == table_name:
                return rows
        return []

    def _cls_for_table(self, table_name: str):
        for cls in self._store:
            if getattr(cls, "__tablename__", None) == table_name:
                return cls
        return None

    @staticmethod
    def _col_value(ns: dict, col):
        """从「表名 → 行对象」命名空间取列值。"""
        obj = ns.get(col.table.name)
        return None if obj is None else getattr(obj, col.key, None)

    def _truthy(self, cond, ns: dict) -> bool:
        left = getattr(cond, "left", None)
        opfn = getattr(cond, "operator", None)
        if left is None or opfn is None:
            raise AssertionError(f"unsupported condition: {cond!r}")
        lval = self._col_value(ns, left)
        right = getattr(cond, "right", None)
        name = getattr(opfn, "__name__", "")

        if name == "in_op":
            return lval in (getattr(right, "value", None) or [])
        if isinstance(right, _SATrue):
            return lval == True  # noqa: E712 — col.is_(True) 的常量右值
        if isinstance(right, _SAFalse):
            return lval == False  # noqa: E712 — col.is_(False) 的常量右值
        if opfn is sa_operators.is_ or opfn is sa_operators.is_not:
            # is_(True)/is_(False) 已被上面短路，走到这里右值只可能是 Null()（判空）
            if not isinstance(right, Null):
                raise AssertionError(f"unsupported is_ right value: {cond!r}")
            return lval is None if opfn is sa_operators.is_ else lval is not None
        if getattr(right, "table", None) is not None and getattr(right, "key", None) is not None:
            rval = self._col_value(ns, right)  # 列对列比较（join onclause）
        else:
            rval = getattr(right, "value", right)
        if opfn is pyop.eq:
            return lval == rval
        if opfn is pyop.ne:
            return lval != rval
        if opfn is pyop.gt:
            return lval > rval
        if opfn is pyop.ge:
            return lval >= rval
        if opfn is pyop.lt:
            return lval < rval
        if opfn is pyop.le:
            return lval <= rval
        raise AssertionError(f"unsupported operator {name}: {cond!r}")

    def _scan_join(self, node) -> list[dict]:
        """join 树（含嵌套）→ 行命名空间列表 [{表名: obj}]。"""
        if isinstance(node, Join):
            out = []
            for lo in self._scan_join(node.left):
                for ro in self._scan_join(node.right):
                    ns = {**lo, **ro}
                    if self._truthy(node.onclause, ns):
                        out.append(ns)
            return out
        return [{node.name: o} for o in self._objs_for_table(node.name)]

    @staticmethod
    def _order_key(v):
        return (v is None, v)

    def _apply_order(self, stmt, ns_rows: list[dict]) -> list[dict]:
        # 从最后一个排序键到第一个逐次稳定排序（Python sort 稳定，等价多键混排）
        for ob in reversed(list(getattr(stmt, "_order_by_clauses", None) or [])):
            col = getattr(ob, "element", ob)
            if getattr(col, "table", None) is None:
                continue
            reverse = getattr(ob, "modifier", None) is not None  # desc_op 非空
            ns_rows.sort(key=lambda ns: self._order_key(self._col_value(ns, col)), reverse=reverse)
        return ns_rows

    def _project(self, stmt, descs, ns_rows: list[dict]):
        entity = descs[0]["entity"] if descs else None
        # func.count（含带列的 count(col)）→ 过滤后行数（GROUP BY 不支持，防恒真）
        if descs and isinstance(descs[0]["expr"], Function):
            if getattr(stmt, "_group_by_clauses", None):
                raise AssertionError(f"unsupported group_by: {stmt}")
            return [len(ns_rows)]
        if not descs:
            raise AssertionError(f"unsupported projection: {stmt}")
        if entity is not None and descs[0]["expr"] is entity:
            rows = [ns[entity.__tablename__] for ns in ns_rows]
        elif len(descs) >= 2:
            cls = entity
            names = [d["name"] for d in descs]
            rows = [tuple(getattr(ns[cls.__tablename__], n, None) for n in names) for ns in ns_rows]
        else:
            cls = entity
            name = descs[0]["name"]
            # 单列标量 → 1-tuple（对齐真实 Row 语义：.all() 供 ``(pid,)`` 解包）
            rows = [(getattr(ns[cls.__tablename__], name, None),) for ns in ns_rows]
        if getattr(stmt, "_distinct", False):
            seen, uniq = set(), []
            for r in rows:
                if r not in seen:
                    seen.add(r)
                    uniq.append(r)
            rows = uniq
        return rows

    def _eval_select(self, stmt):
        descs = stmt.column_descriptions or []
        froms = stmt.get_final_froms()
        # select(func.count()).select_from(inner.subquery()) → 内层行数
        if len(froms) == 1 and isinstance(froms[0], Subquery):
            inner_ns = self._scan_rows(froms[0].element)
            return [len(inner_ns)]
        ns_rows = self._scan_rows(stmt)
        offset = stmt._offset_clause.value if getattr(stmt, "_offset_clause", None) is not None else None
        limit = stmt._limit_clause.value if getattr(stmt, "_limit_clause", None) is not None else None
        if offset:
            ns_rows = ns_rows[offset:]
        if limit is not None:
            ns_rows = ns_rows[:limit]
        return self._project(stmt, descs, ns_rows)

    def _eval_where(self, node, ns: dict) -> bool:
        """WHERE 树递归求值：AND / OR 各按其语义（``BooleanClauseList.operator`` 区分）。

        不能把 ``clauses`` 摊平成一维再 ``all()``：那样 ``or_(A, B)`` 退化成 AND，
        「平台域 = 无归属 ∪ 租户0」这类条件会被过滤成空集——断言会假红，或在只喂了
        单侧行的用例里假绿。
        """
        if node is None:
            return True
        clauses = getattr(node, "clauses", None)
        if clauses is None:
            return self._truthy(node, ns)
        if getattr(node, "operator", None) is pyop.or_:
            return any(self._eval_where(c, ns) for c in clauses)
        return all(self._eval_where(c, ns) for c in clauses)

    def _scan_rows(self, stmt) -> list[dict]:
        froms = stmt.get_final_froms()
        ns_rows = self._scan_join(froms[0])
        where = getattr(stmt, "whereclause", None)
        if where is not None:
            ns_rows = [ns for ns in ns_rows if self._eval_where(where, ns)]
        return self._apply_order(stmt, ns_rows)

    def _execute_select(self, stmt) -> MiniResult:
        return MiniResult(self._eval_select(stmt))

    def _execute_delete(self, stmt) -> MiniResult:
        table = stmt.table
        rows = self._objs_for_table(table.name)
        doomed = [r for r in rows if self._eval_where(stmt.whereclause, {table.name: r})]
        doomed_ids = {id(r) for r in doomed}
        self._store[self._cls_for_table(table.name)] = [r for r in rows if id(r) not in doomed_ids]
        return MiniResult(doomed)
