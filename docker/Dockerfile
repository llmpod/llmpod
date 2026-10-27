# syntax=docker/dockerfile:1.7
# =====================================================================
# LLMPod 容器镜像 —— 单一镜像承载三个角色：管理端（webserver）、API 网关（proxyserver）、节点代理（node）。
#
# 角色由运行时参数决定 (docker-compose 用 command 覆盖):
#   command ["webserver"]   -> 管理端 (manager WS + webserver REST), 默认
#   command ["proxyserver"] -> API 网关 (8090)
#   command ["node"]        -> 节点代理 (反连管理端)
# DB 自举 (建库/建表/种子) 由 entrypoint.sh 在 webserver 角色启动前幂等执行,
# 无独立 db-init 容器; node 角色跳过初始化。手动幂等重跑:
#   docker compose --env-file .env exec webserver python scripts/init_db.py
#
# 设计:
# 1. uv 做 Python 依赖管理 (与 Makefile/CI 一致); uv.lock 随上下文 COPY,
#    --frozen 锁定版本, 构建可复现。
# 2. 两段构建: builder 装依赖导出 venv; runtime 仅拷 venv+源码, 无编译器。
# 3. python:3.12-slim 基础 (requires-python>=3.12)。
# 4. docker.sock 挂载进容器后由 SDK 直连（deployers/docker.get_docker_client
#    唯一入口），容器内不带 docker CLI。
# 5. --extra node (pynvml/psutil/docker) 覆盖管理端+节点两角色所需。
#    API 网关 proxyserver 是 httpx 裸转发（无独立 extra；openai/anthropic SDK
#    仅测试契约依赖，运行时不需要）。
# =====================================================================

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.5.11

# ---------------------------------------------------------------------
# Stage 1: builder —— 用 uv 安装依赖到 /opt/venv
# ---------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ARG UV_VERSION

ENV \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH

# asyncpg 等 wheel 缺预编译时需要构建链; ca-certs/curl 给 pip 装 uv 用
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update -qq && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 固定版本 uv
RUN pip install --no-cache-dir uv==${UV_VERSION}

WORKDIR /app

# 先拷依赖清单 (层缓存) —— 仅 pyproject/uv.lock 变更才重装依赖
COPY pyproject.toml uv.lock ./

# --no-install-project: 装依赖但不装项目本体 (下一步源码 COPY 不触发重装)
RUN uv sync --frozen --no-install-project --no-dev --extra node

# 拷源码并安装项目本体；alembic 迁移脚本一并入镜像（entrypoint DB init 用）
COPY llmpod ./llmpod
COPY scripts ./scripts
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev --extra node

# ---------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG PYTHON_VERSION=3.12
# 镜像版本以发布的 tag 为准；运行时版本读源码 llmpod/__init__.py 的 __version__。
LABEL org.opencontainers.image.title="llmpod" \
      org.opencontainers.image.description="LLMPod —— AI 推理网关平台 (管理端 + API 网关 + 节点代理)" \
      org.opencontainers.image.source="https://github.com/LLMPPOD-Team/llmpod" \
      createdBy="bt_apps"

ENV \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    # 默认数据目录 (节点落 node_id, 管理端落 node_token)
    LLMPOD_NODE_ID_FILE=/app/data/node_id \
    LLMPOD_NODE_TOKEN_FILE=/app/data/node_token

# curl (healthcheck) + tini (PID1 信号转发)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update -qq && apt-get install -y --no-install-recommends \
        tini curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 依赖 + 源码
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

# 容器入口脚本 (按角色决定是否先做 DB 自举)
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/data

# 全角色以 root 运行：节点代理要读写挂载的 /var/run/docker.sock（root:docker 660）、
# 经它创建兄弟推理容器并往宿主模型目录写文件；非特权用户在各机 docker gid / 目录
# 属主上频繁踩权限坑（需 group_add、chown 等额外步骤兜底），root 一律天然可读写。
WORKDIR /app

EXPOSE 8080 8090

# 健康检查: 管理端 /health。node 角色无 HTTP，引导脚本以 --no-healthcheck 关闭；
# proxyserver 角色需在服务级（compose）覆盖健康检查，内置的 8080 探测对它不适用。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# tini 转发信号; entrypoint.sh 按角色做 DB init 后 exec python -m llmpod
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["webserver"]
