"""前端静态资源挂载：把 SPA 构建产物交给管理端 FastAPI 应用一并提供。

支持**多入口(multi-SPA)构建**：dist 根目录是平台入口；每个一级子目录(如
``admin/``、``tenant/``)若含 ``index.html`` 则视为独立 SPA。请求按首段路由到
对应入口的 index.html，资源文件按真实路径返回：

    /                  -> index.html         (平台入口)
    /admin/dashboard    -> admin/index.html   (运营后台 SPA)
    /tenant/profile     -> tenant/index.html  (租户后台 SPA)
    /admin/assets/x.js  -> admin/assets/x.js  (真实文件)

设计要点：
- 优雅降级：未找到根 ``index.html`` 时仅告警、不挂载，服务照常只提供 API。
- SPA 兜底路由必须**最后**注册，优先级低于 ``/api/v1``、``/ws/node``、``/health``、``/docs``。
- 路径穿越防护；各 SPA 的 ``assets/`` 给不可变缓存，``index.html`` 走 no-cache。
- 全局设置下发：返回任一 index.html 时在 ``<head>`` 后、前端首屏启动脚本前注入
  同步脚本，把全局设置挂到 ``window.__LLMPOD_SETTINGS__``（契约 = ``GET /api/v1/settings``
  的 ``data`` 原样 + 只读附加键 ``serverVersion`` / ``first_guide``）。后端不感知前端
  任何存储细节，由前端 boot 脚本决定如何消费。``first_guide``（首次引导弹窗）
  的弹出机会随首个携带待弹出态的管理端 index.html 服务消耗（见
  first_guide_service）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import anyio
import llmpod
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from llmpod.config.settings import settings
from llmpod.db import get_db_context
from llmpod.services.first_guide_service import (
    FIRST_GUIDE_KEY,
    consume_first_guide,
    first_guide_open,
)
from llmpod.services.settings_service import SettingsPayload, get_settings_or_none

logger = logging.getLogger("llmpod.apps.webserver.frontend")

# 内容哈希静态资源可缓存一年（新版靠文件名哈希刷新）
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
_NOCACHE = "no-cache"

# <head> 开标签（大小写不敏感，取首次匹配）——注入脚本插在其后、内联启动脚本之前
_HEAD_OPEN_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)

# 全局设置下发挂载的全局变量名（前端 boot 脚本按此读取；值形态 = 公开读接口的 data）
_SETTINGS_VAR = "window.__LLMPOD_SETTINGS__"


def _resolve_dist_dir() -> Path:
    """解析前端构建产物目录。

    优先使用 ``settings.frontend_dist``；为空时回退到包内 ``llmpod/static/``。
    """
    if settings.frontend_dist:
        return Path(settings.frontend_dist).expanduser()
    # 默认：包内 llmpod/static/，与运行时 CWD 无关
    return Path(llmpod.__file__).resolve().parent / "static"


def _is_asset(full_path: str) -> bool:
    """是否为内容哈希静态资源（任意 SPA 的 ``assets/`` 子目录下）。"""
    return full_path.startswith("assets/") or "/assets/" in full_path


def _cache_headers(full_path: str) -> dict[str, str]:
    """``assets/`` 下不可变缓存，其余（index.html / logo 等）no-cache。"""
    return {"cache-control": _IMMUTABLE_CACHE if _is_asset(full_path) else _NOCACHE}


def _json_compact(obj: dict) -> str:
    """紧凑 JSON，``<`` 转义为 ``\\u003c`` 防 ``</script>`` 逃逸。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _build_seed_script(payload, first_guide: bool) -> str:
    """据全局设置构建 ``window.__LLMPOD_SETTINGS__`` 下发脚本（同步执行）。

    变量主体 = ``GET /api/v1/settings`` 的 data 原样（品牌/主题/登录页/维护，
    首屏渲染与登录页横幅靠它），外加两个只读附加键：``serverVersion``（运行时
    平台版本，关于页/页脚展示）与 ``first_guide``（首次引导弹窗是否待弹出，
    管理端登录落地守卫读，见 first_guide_service）。两键只经注入下发、不进
    设置 API 契约，前端均从 window 变量读、不用另调接口。

    取值规则（``null`` 是「管理员未配置」的清缓存信号，属设置域规则）：

    - 有任何已配置字段（维护开关/文案也算）→ 完整 payload + 附加键；
    - 配置全空 → ``null``，前端据此清缓存回落各自默认值（此时不带版本，
      前端拿版本走 ``GET /api/v1/version/latest``）；
    - 唯一例外——配置全空但首次引导弹窗待弹出：仍注入对象。引导是系统
      状态，不随「管理员清空设置」消失，而置 null 会把守卫要读的变量整个
      清掉；boot 桥接对全空对象与 null 等价（空 seed 同样清缓存），品牌/
      主题不受影响；
    - DB 读失败由调用方直接不注入（变量保持 undefined，前端沿用本地缓存）。
    """
    values = payload.model_dump()
    if not first_guide and values == SettingsPayload().model_dump():
        literal = "null"
    else:
        values["serverVersion"] = llmpod.__version__
        values[FIRST_GUIDE_KEY] = first_guide
        literal = _json_compact(values)
    return f"<script>{_SETTINGS_VAR}={literal};</script>"


async def _guide_flag_for_seed(serving_admin: bool) -> bool:
    """读首次引导弹窗状态供种子注入；管理端入口且待弹出时随读消耗弹出机会。

    独立短会话，不与全局设置共用（防读失败毒化的事务连累设置种子收尾）。
    消耗语义见 first_guide_service：本页读到的是消耗前的待弹出态、仍注入
    ``true``，此后永久不再弹。非管理端入口只读不耗——守卫只在管理端，
    在别处消耗等于白白把仅有的一次弹出机会弹掉。三层失败各归各吞（读侧
    在 first_guide_open 内、消耗侧在 consume_first_guide 内、会话收尾在此处）。
    """
    try:
        async with get_db_context() as db:
            guide_open = await first_guide_open(db)
            if guide_open and serving_admin:
                await consume_first_guide(db)
            return guide_open
    except Exception as exc:
        logger.warning("读取首次引导弹窗状态的独立会话失败，按不弹处理：%s", exc)
        return False


async def _serve_index(index_path: Path, serving_admin: bool) -> Response:
    """读 index.html 并注入全局设置下发脚本后返回（no-cache）。

    全局设置读失败（DB 异常且无缓存）→ 原样返回不注入：前端 boot 读不到
    变量，沿用本地缓存，页面照常可用。全局设置读取走 TTL 缓存；引导状态为
    保证首登后立即可见不走缓存且不与设置共会话（登录与注入同进程，时序上
    紧邻）；HTML 磁盘读走线程池（原 FileResponse 语义，避免占住管理端事件循环）。
    """

    def _plain() -> Response:
        return Response(content=html, media_type="text/html", headers={"cache-control": _NOCACHE})

    # 磁盘读放线程池：避免阻塞同进程的 /api/v1 与 /ws/node（原 FileResponse 经线程读文件）
    html = await anyio.to_thread.run_sync(index_path.read_bytes)
    try:
        async with get_db_context() as db:
            payload = await get_settings_or_none(db)
    except Exception as exc:
        logger.warning("全局设置种子注入跳过（读取失败，返回原始页面）：%s", exc)
        return _plain()
    if payload is None:
        return _plain()
    match = _HEAD_OPEN_RE.search(html)
    if match is None:
        return _plain()
    seed = _build_seed_script(payload, await _guide_flag_for_seed(serving_admin)).encode("utf-8")
    injected = html[: match.end()] + seed + html[match.end() :]
    return Response(content=injected, media_type="text/html", headers={"cache-control": _NOCACHE})


def mount_frontend(app: FastAPI) -> None:
    """在 app 上挂载前端静态资源（含多 SPA 兜底路由）。必须最后调用。"""
    if not settings.serve_frontend:
        logger.info("已禁用前端静态资源服务（serve_frontend=False），仅提供 API。")
        return

    dist = _resolve_dist_dir()
    root_index = dist / "index.html"
    if not root_index.is_file():
        logger.warning("前端构建产物未找到(%s)，仅提供 API。", dist)
        return

    dist_resolved = dist.resolve()

    # 发现各 SPA 入口：一级子目录含 index.html 即视为独立 SPA（admin/、tenant/…）
    app_indices: dict[str, Path] = {}
    for child in dist.iterdir():
        if child.is_dir():
            idx = child / "index.html"
            if idx.is_file():
                app_indices[child.name] = idx
    entries = ["/"] + [f"/{name}" for name in app_indices]
    logger.info("挂载前端静态资源: %s（SPA 入口: %s）", dist, ", ".join(entries))

    # 全部 SPA 入口（含根）：命中即走种子注入路径，防直访 /admin/index.html 旁路无种子
    index_files = {root_index.resolve(), *(idx.resolve() for idx in app_indices.values())}

    def index_for(full_path: str) -> Path:
        """按请求首段选择对应 SPA 的 index.html，缺省回退根入口。"""
        first = full_path.split("/", 1)[0] if full_path else ""
        return app_indices.get(first, root_index)

    # admin SPA 入口——首次引导弹窗只在管理端消耗（守卫只在 admin 消费 first_guide）
    admin_index = app_indices.get("admin")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # API 路径未命中不应返回 HTML：交给全局异常处理 → Result JSON
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        # 真实文件（各 SPA 的 assets/logo/images 等），带路径穿越防护
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_relative_to(dist_resolved) and candidate.is_file():
                if candidate in index_files:
                    # 直访 index.html：与 SPA 兜底同路径（含种子注入），缓存策略一致
                    return await _serve_index(candidate, serving_admin=candidate == admin_index)
                return FileResponse(candidate, headers=_cache_headers(full_path))
        # 前端路由 -> 对应 SPA 的 index.html（history 模式深链刷新）
        index = index_for(full_path)
        return await _serve_index(index, serving_admin=index == admin_index)
