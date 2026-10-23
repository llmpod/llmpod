"""新增路由集成测试共用的 FakeSession 增强：按运算符语义过滤 + 单列投影 1 元组。

既有 ``_fakes.FakeSession`` 只做等值/IN 过滤（忽略 ``!=``/``is_``/比较运算的语义），
且单列投影（``select(Entity.col)``）返回裸标量。而路由层惯用
``{rid for (rid,) in (await db.execute(...)).all()}`` 元组解包与
「``user_id != 目标用户`` 计数」（最后一个管理员守卫），既有桩形状覆盖不到。

本子类**不改动既有文件**（既有测试依赖 FakeSession 当前形状），仅供本目录
新增测试使用：

- where 过滤按 ``cond.operator`` 分发（eq / ne / in_ / is_ / gt / ge / lt / le）；
- 单列投影返回 1 元组行，匹配路由层 tuple 解包惯用法（``func.count()`` 等聚合
  分支仍按基类返回标量，不受影响）；
- 支持 ``offset``（分页 list 端点 page>1）。

join 仍不支持（与基类一致）：涉及 join 的 service（get_role_permissions 等）
沿用项目惯例在路由模块命名空间 monkeypatch。
"""

from __future__ import annotations

import operator as _op
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.functions import Function
from sqlalchemy.sql.selectable import Select

from _fakes import FakeSession, _FakeResult


def _op_name(fn) -> str:
    return getattr(fn, "__name__", "")


def _matches(obj, cond) -> bool:
    """单条件判定：按运算符语义过滤；无法解析的条件不过滤（防误杀）。"""
    if not hasattr(cond, "left") or not hasattr(cond.left, "key"):
        return True
    left = getattr(obj, cond.left.key, None)
    fn = getattr(cond, "operator", None)
    right = getattr(cond, "right", None)
    val = getattr(right, "effective_value", None) if right is not None else None
    if val is None and right is not None:
        val = getattr(right, "value", right)
    name = _op_name(fn)
    if fn is _op.eq:
        return left == val
    if fn is _op.ne:
        return left != val
    if name == "in_op":
        return left in (val or [])
    if name == "not_in_op":
        return left not in (val or [])
    if name == "is_":
        # SQL 的 IS 语义：col.is_(None) 判 NULL；col.is_(True())/False() 判布尔身份。
        # val 拿到的是 BindParameter/常量对象——None 直接比，True_/False_ 取 .value。
        if right is None:
            return True
        bound = getattr(right, "value", None)
        if bound is None:
            return left is None
        return left is bound or left == bound
    if name == "is_not":
        if right is None:
            return True
        bound = getattr(right, "value", None)
        if bound is None:
            return left is not None
        return not (left is bound or left == bound)
    if fn is _op.gt:
        return left is not None and left > val
    if fn is _op.ge:
        return left is not None and left >= val
    if fn is _op.lt:
        return left is not None and left < val
    if fn is _op.le:
        return left is not None and left <= val
    return True  # 未知运算符（like 等）：不过滤


def _apply_server_defaults(obj) -> None:
    """flush 时按列的 server_default 回填 None 属性（模拟真实 PG INSERT 后回读）。

    FakeSession 不会触发 server_default（status/created_at/updated_at 等），
    直接序列化会得到 null 撞 response 校验。仅回填「Python 侧为 None 且列声明了
    server_default」的字段，语义与 INSERT 后 RETURNING 一致。
    """
    for col in obj.__table__.columns:
        if getattr(obj, col.key, None) is not None:
            continue
        sd = getattr(col, "server_default", None)
        if sd is None:
            continue
        arg = getattr(sd, "arg", None)
        type_name = str(col.type).upper()
        if hasattr(arg, "name") or "Function" in type(type(arg)).__name__ or arg is None:
            # func.now() 等 SQL 函数：时间列回当前时刻
            if "TIMESTAMP" in type_name or "DATETIME" in type_name:
                setattr(obj, col.key, datetime.now(timezone.utc))
            continue
        text = str(arg)
        try:
            if "BOOL" in type_name:
                setattr(obj, col.key, text.lower() in ("true", "t", "1"))
            elif "NUMERIC" in type_name or "DECIMAL" in type_name:
                setattr(obj, col.key, Decimal(text))
            elif "INT" in type_name:
                setattr(obj, col.key, int(text))
            else:
                setattr(obj, col.key, text)
        except (ValueError, TypeError):
            pass


class OpSession(FakeSession):
    """运算符感知的 FakeSession（说明见模块 docstring）。"""

    async def flush(self):
        for obj in list(self._staged):
            _apply_server_defaults(obj)
        await super().flush()

    async def execute(self, stmt):
        if isinstance(stmt, (Update, Delete)):
            return self._execute_literal(stmt)
        if not isinstance(stmt, Select):
            return await super().execute(stmt)

        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        if entity is None:
            # select(func.count()).select_from(X)：按表名回查实体（同基类）
            for f in getattr(stmt, "get_final_froms", lambda: [])() or []:
                entity = self._cls_for_table(getattr(f, "name", ""))
                if entity is not None:
                    break
        objs = list(self._store.get(entity, {}).values())
        for cond in _flatten_and(stmt.whereclause):
            objs = [o for o in objs if _matches(o, cond)]

        # order_by（含 desc 的 UnaryExpression；多键靠稳定排序叠加）
        for ob in reversed(getattr(stmt, "_order_by_clauses", None) or []):
            col = getattr(ob, "element", ob)
            name = getattr(col, "key", None)
            if not name:
                continue
            objs = sorted(
                objs,
                key=lambda o: (getattr(o, name, None) is None, getattr(o, name, None)),
                reverse=_op_name(getattr(ob, "modifier", None)) == "desc_op",
            )
        if getattr(stmt, "_offset_clause", None) is not None:
            objs = objs[int(stmt._offset_clause.value or 0) :]
        if getattr(stmt, "_limit_clause", None) is not None:
            objs = objs[: int(stmt._limit_clause.value or 0)]

        descs = stmt.column_descriptions or []
        if descs and isinstance(descs[0].get("expr"), Function):
            return _FakeResult([len(objs)])
        # 单列投影 → 1 元组行（路由层 `for (x,) in rows.all()` 解包惯例）
        if len(descs) == 1 and descs[0].get("expr") is not entity:
            name = descs[0]["name"]
            return _FakeResult([(getattr(o, name, None),) for o in objs])
        if len(descs) >= 2:
            names = [d["name"] for d in descs]
            return _FakeResult([tuple(getattr(o, n, None) for n in names) for o in objs])
        return _FakeResult(objs)

    def _cls_for_table_name(self, table):
        for c in self._store:
            t = getattr(c, "__table__", None)
            if t is not None and t.name == table.name:
                return c
        return None

    def _execute_literal(self, stmt):
        """字面量 update()/delete()：全 AND 条件按运算符语义匹配（复合 where 不再退化为全表）。"""
        cls = self._cls_for_table_name(stmt.table)
        if cls is None:
            return _FakeResult([])
        store = self._store.setdefault(cls, {})
        conds = _flatten_and(stmt.whereclause)
        doomed = [pk for pk, o in store.items() if all(_matches(o, c) for c in conds)]
        if isinstance(stmt, Delete):
            for pk in doomed:
                store.pop(pk, None)
            return _FakeResult([])
        values = getattr(stmt, "_values", None) or {}
        for pk in doomed:
            obj = store[pk]
            for col, val in values.items():
                key = col.key if hasattr(col, "key") else col
                setattr(obj, key, getattr(val, "value", val))
        return _FakeResult([store[pk] for pk in doomed])


def _flatten_and(wc):
    """AND 链扁平化为单条件列表。"""
    clauses = getattr(wc, "clauses", None)
    if clauses is not None:
        out: list = []
        for c in clauses:
            out.extend(_flatten_and(c))
        return out
    return [wc] if wc is not None else []
