"""首次引导弹窗：管理端「一键部署向导」的一次性弹出开关。

系统首次平台超管成功登录后，管理端首次落地时前端弹一次部署向导，此后
永久不再弹（向导仍可从部署页空态等入口手动进入）。两个一次性动作，各一
个触发点：

- **触发**（``open_first_guide``）：系统首次平台超管成功登录（门控在登录
  路由，租户/非超管不参与）→ 置 ``{"state": "open"}``（待弹出）。此后种子
  注入携带 ``first_guide=true``，管理端路由守卫把登录落地改道向导页；
- **消耗**（``consume_first_guide``）：首个携带待弹出态的管理端 index.html
  服务 → 置 ``{"state": "consumed"}``（弹过即永久关闭）。弹出机会整个系统
  生命周期只有一次——跳过/完成后换标签页、换浏览器都不会再弹。

状态必须落库而非按「是否有人登录过」现推：登录页跳管理端是跨 SPA 整页
加载，注入时点在登录之后，现推会在首登提交后立刻翻 False，向导一次都
弹不出来。

状态存 ``SystemConfig`` 单 key ``"first_guide"``：无行 = 从未触发；
``{"state": "open"}`` = 待弹出；``{"state": "consumed"}`` = 已弹过（终态）。
触发者由行 ``updated_by`` 记录、消耗时间即行 ``updated_at``，value 内不重复。
非 open 的一切 value——空对象、未知字面量、非 dict（通用 config 接口可写
任意结构、手工 SQL 产物）——读作不弹、写侧不碰行，无法重新拉起或预写
压制。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models.system_config import SystemConfig

log = logging.getLogger("llmpod.services.first_guide")

# SystemConfig 存储 key（与注入到前端的只读附加键同名）
FIRST_GUIDE_KEY = "first_guide"

_STATE_OPEN = "open"
_STATE_CONSUMED = "consumed"


def _is_open(value: object) -> bool:
    """value 是否为待弹出态；非 dict（手工 SQL / 备份恢复产物）按不弹处理。"""
    return isinstance(value, dict) and value.get("state") == _STATE_OPEN


async def _get_guide_row(db: AsyncSession) -> SystemConfig | None:
    return (await db.execute(select(SystemConfig).where(SystemConfig.key == FIRST_GUIDE_KEY))).scalar_one_or_none()


async def first_guide_open(db: AsyncSession) -> bool:
    """首次引导弹窗是否待弹出。

    行缺失、value 非法、DB 读失败一律按不弹返回：引导是锦上添花，不该让它
    的故障连累设置种子注入（注入侧另开独立短会话读，与设置读取互不传染）。
    """
    try:
        row = await _get_guide_row(db)
    except Exception as exc:
        log.warning("读取首次引导弹窗状态失败，按不弹处理：%s", exc)
        return False
    return row is not None and _is_open(row.value)


async def open_first_guide(db: AsyncSession, *, user_id: int) -> None:
    """系统首次超管登录触发首次引导弹窗（幂等）：已有行（含终态、非法）一律不动。

    写入包在 SAVEPOINT 里：并发首登撞主键或任何落库异常只回滚到保存点，
    绝不 ``rollback()`` 调用方的 session（同事务里还有 ``authenticate`` 写的
    ``last_login_at``，卷走即丢）。只 ``flush``，``commit`` 归路由层。
    """
    try:
        row = await _get_guide_row(db)
        if row is not None:
            return
        async with db.begin_nested():
            db.add(
                SystemConfig(
                    key=FIRST_GUIDE_KEY,
                    value={"state": _STATE_OPEN},
                    description="首次引导状态",
                    updated_by=user_id,
                )
            )
            await db.flush()
    except Exception as exc:
        log.warning("首次引导弹窗触发失败（不影响登录）：%s", exc)


async def consume_first_guide(db: AsyncSession) -> None:
    """首次引导弹窗弹过即永久关闭（幂等）：首个携带待弹出态的管理端页面
    服务时消耗本次弹出机会，之后换标签页/浏览器都不再弹。

    终态/无行/非法 value 直接跳过。失败自吞（含 SAVEPOINT 回滚到保存点，
    防止毒化的 flush 让本会话收尾 commit 报错）——关闭失败保持待弹出，
    下次管理端落地再关，不影响本次页面已注入的 ``true``。
    """
    try:
        async with db.begin_nested():
            row = await _get_guide_row(db)
            if row is None or not _is_open(row.value):
                return
            row.value = {"state": _STATE_CONSUMED}
            await db.flush()
    except Exception as exc:
        log.warning("关闭首次引导弹窗失败（保持待弹出，下次再关）：%s", exc)
