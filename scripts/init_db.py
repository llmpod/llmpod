"""数据库初始化脚本: 建库 + 同步 schema（Alembic 版本化） + 填充初始数据。

此脚本:
1. 如果 llmpod 数据库不存在则创建
2. 同步表结构到当前模型（空库 create_all 一次成型并打版本戳；存量库走
   Alembic 迁移链增量升级——此后模型变更必须附带新 revision）
3. 填充初始数据: 平台租户、管理员用户、默认角色
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine
from llmpod.config.settings import settings
from llmpod.core.security import hash_password
from llmpod.db import init_all_dbs, close_all_dbs, get_db_context, Base
from llmpod.db.pg import get_engine
from llmpod.models.cluster import Cluster
from llmpod.models.tenant import Tenant
from llmpod.models.role import Role
from llmpod.models.user import User
from llmpod.models.user_role import UserRole

# 仓库根目录（含 alembic.ini）；定位与 CWD 无关，容器 /app 与开发仓库根都成立
_REPO_ROOT = Path(__file__).resolve().parent.parent

# 随机引导凭据落盘路径（相对 CWD：容器 /app/data 与开发仓库根 data/ 均为数据目录）
_ADMIN_CREDENTIALS_FILE = Path("data/admin_credentials")


def write_admin_credentials_file(username: str, password: str) -> None:
    """随机生成的管理员凭据落盘（chmod 600，JSON 与登录请求体同构，可直接 curl -d）。

    写失败直接抛异常中止 init——随机凭据若不可找回，宁可起不来也不能锁死。
    """
    path = _ADMIN_CREDENTIALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"username": username, "password": password}, ensure_ascii=False) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    print(f"  随机管理员凭据已生成并写入 {path}（容器内 /app/data/admin_credentials），首次登录后请在管理后台改密")


async def create_database_if_not_exists():
    """如果 llmpod 数据库和用户不存在则创建。

    使用 postgres 超级用户创建数据库/用户，然后切换到 llmpod 用户。
    """
    # 使用 postgres 超级用户创建数据库和用户
    # 优先使用环境变量，其次使用常见默认值
    pg_admin_user = settings.pg_user
    pg_admin_password = settings.pg_password
    if not pg_admin_password:
        raise RuntimeError("LLMPOD_PG_PASSWORD 未配置（请在 .env 或环境变量中设置）")

    admin_url = (
        f"postgresql+asyncpg://{pg_admin_user}:{pg_admin_password}@{settings.pg_host}:{settings.pg_port}/postgres"
    )
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")

    try:
        async with admin_engine.connect() as conn:
            # 标识符/密码转义（PG DDL 不支持 bind parameters for identifiers）
            escaped_pwd = settings.pg_password.replace("'", "''")
            escaped_user = settings.pg_user.replace("'", "''")
            escaped_db = settings.pg_database.replace('"', '""')

            # 如果用户不存在则创建
            result = await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :rolname"),
                {"rolname": settings.pg_user},
            )
            if not result.scalar():
                print(f"正在创建用户: {settings.pg_user}")
                await conn.execute(text(f"CREATE USER {escaped_user} WITH PASSWORD '{escaped_pwd}'"))
                print("用户创建成功")
            else:
                print(f"用户已存在: {settings.pg_user}")

            # 如果数据库不存在则创建
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :datname"),
                {"datname": settings.pg_database},
            )
            if not result.scalar():
                print(f"正在创建数据库: {settings.pg_database}")
                await conn.execute(text(f'CREATE DATABASE "{escaped_db}" OWNER {escaped_user}'))
                print("数据库创建成功")
            else:
                print(f"数据库已存在: {settings.pg_database}")

            # 授予权限
            await conn.execute(text(f'GRANT ALL PRIVILEGES ON DATABASE "{escaped_db}" TO {escaped_user}'))
            print("权限已授予")
    finally:
        await admin_engine.dispose()


def _alembic_config() -> Config:
    """构造指向仓库 alembic.ini 的 Alembic 配置（URL 由 env.py 从 Settings 取）。"""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


def _known_revisions(cfg: Config) -> set[str]:
    """当前代码迁移链里的全部 revision id（不含 DB 状态）。"""
    from alembic.script import ScriptDirectory

    return {rev.revision for rev in ScriptDirectory.from_config(cfg).walk_revisions()}


async def get_db_revision(engine, table_names: set[str] | None = None) -> str | None:
    """读库里的 schema 版本戳；无 alembic_version 表（1.0.0 时代老库）返回 None。

    ``table_names`` 传入调用方已 inspect 过的表名集合可省一次 catalog 往返。
    """
    if table_names is None:
        from sqlalchemy import inspect

        async with engine.connect() as conn:
            table_names = set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))
    if "alembic_version" not in table_names:
        return None
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return result.scalar_one_or_none()


async def sync_schema():
    """把数据库表结构同步到当前代码版本。

    - 空库（除 alembic_version 外无任何表）：按当前 ORM 模型 create_all
      一次成型，并 stamp 到迁移链头部——新库不回放历史 DDL；
    - 存量库：先校验库里的版本戳在当前代码迁移链内（不在 = 旧迁移链残留或
      镜像被降级，拒绝启动防脏写），再 alembic upgrade head 增量升级。
      无版本戳的老库会从 base 回放基线（no-op，只盖戳不建表）后落入基线态。
    """
    print("\n正在同步表结构...")

    # 导入所有模型以注册到 Base.metadata
    from llmpod import models  # noqa: F401 - 导入以注册所有模型到 Base.metadata
    from sqlalchemy import inspect

    engine = get_engine()
    cfg = _alembic_config()
    # 用 inspector 判定空库：除 alembic_version 外无任何业务表
    async with engine.connect() as conn:
        table_names = set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))

    business_tables = table_names - {"alembic_version"}
    if not business_tables:
        print("  空库：按当前模型 create_all 一次成型...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await asyncio.to_thread(command.stamp, cfg, "head")
        print("  已建表并 stamp 到迁移链头部")
    else:
        db_revision = await get_db_revision(engine, table_names)
        if db_revision is not None and db_revision not in _known_revisions(cfg):
            print(
                f"\n错误: 数据库 schema 版本戳 {db_revision} 不在本镜像的迁移链内。\n"
                f"      旧迁移链残留或镜像被降级，都不支持自动收敛（平台只向前升级）。\n"
                f"      停服后 make db-reset 重建；或拉不低于当前 schema 的镜像再启动。"
            )
            sys.exit(1)
        print("  存量库：执行 Alembic 迁移（upgrade head）...")
        await asyncio.to_thread(command.upgrade, cfg, "head")
        print("  迁移完成")

    print("表结构同步成功")


async def record_boot_version_step():
    """对账上次运行版本（SystemConfig system_version）与当前镜像版本。

    升级/降级是用户在宿主机 pull 新镜像后重启发生的，容器内只能事后检测：
    这里把结果打进启动日志（docker logs 可见），升级/降级另落一条 system
    审计（admin 审计日志页可见），无论哪种方向都把当前版本写回留档。
    """
    from llmpod.services.version_service import record_boot_version
    from llmpod.services.audit_service import write_audit

    async with get_db_context() as db:
        boot = await record_boot_version(db)

    if boot.direction in ("upgrade", "downgrade"):
        if boot.direction == "upgrade":
            print(f"  ⬆ 检测到升级: v{boot.previous_version} -> v{boot.current_version}")
        else:
            print(
                f"  ⚠ 检测到版本回退: v{boot.previous_version} -> v{boot.current_version}"
                "（schema 已兼容才走到这，注意功能差异）"
            )
        await write_audit(
            actor_type="system",
            action=boot.direction,
            resource_type="system",
            resource_id=boot.current_version,
            detail={"from": boot.previous_version, "to": boot.current_version},
        )
    elif boot.direction == "initial":
        print(f"  版本记录初始化: v{boot.current_version}")
    else:
        print(f"  版本未变化: v{boot.current_version}")


async def seed_initial_data():
    """填充初始数据: 平台租户、角色、管理员用户。"""
    print("\n正在填充初始数据...")

    async with get_db_context() as db:
        # 1. 创建平台租户（id=0）
        stmt = select(Tenant).where(Tenant.id == 0)
        result = await db.execute(stmt)
        platform_tenant = result.scalar_one_or_none()

        if not platform_tenant:
            print("  正在创建平台租户（id=0）...")
            platform_tenant = Tenant(id=0, name="平台租户", status="active")
            db.add(platform_tenant)
            await db.flush()

        # 2. 创建内置角色（平台域：tenant_id=0；租户域内置角色在租户开通时由 seed_tenant_builtin_roles 创建）
        # name 为展示名（中文），code 为身份标识（不可改）；与租户侧内置角色
        # （租户管理员/租户运营）同风格命名。
        roles_to_create = [
            {"tenant_id": 0, "name": "超级管理员", "code": "super_admin", "is_builtin": True},
            {"tenant_id": 0, "name": "平台运维", "code": "operator", "is_builtin": True},
        ]

        for role_data in roles_to_create:
            stmt = select(Role).where(
                Role.tenant_id == role_data["tenant_id"],
                Role.code == role_data["code"],
            )
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                print(f"  正在创建角色: {role_data['name']}...")
                db.add(Role(**role_data))

        await db.flush()

        # 3. 权限与角色授权（从 permission 注册表自动 seed）
        #    权限常量在 llmpod.permission.platform / tenant 自声明并登记进注册表；
        #    本调用幂等 upsert 全部权限并按全局 BUILTIN_ROLE_PERMISSIONS 授权平台内置角色
        #    （operator 等）。super_admin 靠 is_superuser 短路，无需逐条授权。
        from llmpod.permission.seed import seed_permissions

        print("  正在同步权限与角色授权...")
        await seed_permissions(db)
        print("  权限/角色授权已同步")

        # 4. 管理员引导（凭据仅首次建库生效：平台还没有超管时才创建）
        #    来源 LLMPOD_ADMIN_USER / LLMPOD_ADMIN_PASSWORD——有值用值；空则随机生成，
        #    随机凭据落 data/admin_credentials（JSON 与登录请求体同构，可直接喂 curl -d）。
        #    建库后 env 即失效：改密/改名一律走管理后台，env 改动不会种新号、也不会覆盖密码。
        stmt = (
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.tenant_id == 0, Role.code == "super_admin")
            .limit(1)
        )
        result = await db.execute(stmt)
        existing_admin = result.scalar_one_or_none()

        env_user = settings.admin_user.strip()
        env_password = settings.admin_password.strip()
        if existing_admin is not None:
            print(f"  管理员已存在（username={existing_admin.username}），跳过引导")
            if env_user or env_password:
                print("  提示：LLMPOD_ADMIN_USER/LLMPOD_ADMIN_PASSWORD 仅首次建库生效，本次已忽略")
        else:
            # 获取 super_admin 角色（角色种子在上一步已保证存在）
            stmt = select(Role).where(Role.tenant_id == 0, Role.code == "super_admin")
            result = await db.execute(stmt)
            super_admin_role = result.scalar_one_or_none()

            if not super_admin_role:
                print("  警告: 未找到 super_admin 角色，跳过管理员用户创建")
            else:
                username = env_user or f"admin_{secrets.token_hex(4)}"
                password = env_password or secrets.token_urlsafe(16)
                if env_password and env_password == "admin":
                    print("⚠️  警告：管理员密码为弱口令 'admin'，生产环境请更换强密码")
                if not env_user or not env_password:
                    write_admin_credentials_file(username, password)

                admin_user = User(
                    username=username,
                    password_hash=hash_password(password),
                    email="",
                    tenant_id=0,
                    status="active",
                )
                db.add(admin_user)
                await db.flush()

                # 建立管理员与 super_admin 角色的关联（多对多）
                db.add(UserRole(user_id=admin_user.id, role_id=super_admin_role.id))
                await db.flush()
                print(f"  管理员用户已创建（username={username}）")

        # 5. 按名称查找默认集群，ID 由数据库分配。
        from llmpod.services.cluster_service import get_cluster_by_name

        cluster = await get_cluster_by_name(db, "default")
        if cluster is None:
            print("  正在创建默认集群...")
            cluster = Cluster(
                name="default",
                type="docker",
                description="默认集群",
                status="active",
            )
            db.add(cluster)
            await db.flush()
        print(f"  默认集群 ID：{cluster.id}")

    print("初始数据填充成功")


async def main():
    """主初始化流程。"""
    print("=" * 60)
    print("LLMPod 数据库初始化")
    print("=" * 60)

    print("\n配置:")
    print(f"  PostgreSQL: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
    print(f"  Redis: {settings.redis_host}:{settings.redis_port}")
    print("")

    try:
        # 1. 如果数据库不存在则创建
        await create_database_if_not_exists()

        # 2. 初始化连接
        print("\n正在初始化数据库连接...")
        await init_all_dbs(settings)

        # 3. 同步表结构（空库建表打戳 / 存量库迁移）
        await sync_schema()

        # 4. 记录本次启动版本（升级/降级检测对账，见 version_service）
        await record_boot_version_step()

        # 5. 填充初始数据
        await seed_initial_data()

        print("\n" + "=" * 60)
        print("数据库初始化完成!")
        print("=" * 60)
        print("=" * 60)

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        await close_all_dbs()


if __name__ == "__main__":
    asyncio.run(main())
