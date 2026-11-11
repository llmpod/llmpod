"""登记审计 resource_id 快照的单测（纯函数，不触库/网络）。

钉三件事：

- 短清单原样逗号串（不添尾注）；
- 超过 audit_log.resource_id 列宽 64 字符时收成「前几个…共N个」，总长不超 64，
  且不在数字中间截断、保留的是请求顺序的前缀；
- 空清单返回空串（审计侧 '' 归一化为 NULL）。
"""

from __future__ import annotations

from llmpod.apps.webserver.routes.admin.endpoints import _created_ids_snapshot


def test_short_list_untouched():
    assert _created_ids_snapshot([1, 2, 3]) == "1,2,3"


def test_empty_list_returns_empty_string():
    assert _created_ids_snapshot([]) == ""


def test_exactly_column_width_untouched():
    # 31 个 "1" + "23"：34 个数字字符 + 31 个逗号 = 恰好 64
    ids = [1] * 31 + [23]
    assert len(_created_ids_snapshot(ids)) == 64
    assert _created_ids_snapshot(ids) == ",".join(str(i) for i in ids)


def test_overflow_truncated_to_fit_column():
    # 40 个 6 位 id：全串 279 字符，必超宽；截断后仍是完整数字的前缀 + 尾注
    ids = list(range(100000, 100040))

    snapshot = _created_ids_snapshot(ids)

    assert len(snapshot) <= 64
    assert snapshot.endswith("…共40个")
    prefix = snapshot[: -len("…共40个")]
    parts = prefix.split(",")
    # 前缀是完整数字（无半个数字），且是请求顺序的前几个
    assert all(p.isdigit() and len(p) == 6 for p in parts)
    assert parts == [str(i) for i in ids[: len(parts)]]


def test_one_over_width_truncates():
    # 65 字符（恰超 1）也走截断路径
    ids = [1] * 31 + [233]

    snapshot = _created_ids_snapshot(ids)

    assert len(snapshot) <= 64
    assert snapshot.endswith("…共32个")
    assert snapshot.split(",")[0] == "1"
