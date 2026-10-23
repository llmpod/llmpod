#!/bin/sh
# LLMPod 容器入口: 按角色决定是否先做 DB 自举 / metrics token 初始化, 再 exec 接管为 PID1。
#
#   command ["webserver"]    -> 先幂等建库/建表/种子 (scripts/init_db.py), 再起管理端
#   command ["node"]         -> 跳过初始化, 直接起节点代理 (不连管理库做 DDL)
#   command ["proxyserver"]  -> 跳过 (API 网关不拥有 schema)
#
# 仅 webserver 角色拥有 DB schema, 故只对它跑 init; 其余角色直接 exec。
# init 失败则容器起不来 (set -e) —— 空 DB 跑管理端只会暴露不一致状态。
set -e

ROLE="${1:-webserver}"

# webserver 与 proxyserver 之间的唯一内部 HTTP 是指标抓取 (scraper -> /metrics,
# bearer token 门)。token 不暴露为用户配置: 首启自动生成、持久化在共享数据卷,
# 谁先起谁生成、后来者读同值 (compose 下 proxy 经 depends_on 必然后起);
# 写入走临时文件 + 原子 rename, 乱序启动也不会读到半截内容。
ensure_metrics_token() {
    TOKEN_FILE=/app/data/metrics_token
    if [ -s "$TOKEN_FILE" ]; then
        export LLMPOD_METRICS_TOKEN="$(cat "$TOKEN_FILE")"
    else
        TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
        printf '%s' "$TOKEN" > "${TOKEN_FILE}.tmp"
        chmod 600 "${TOKEN_FILE}.tmp"
        mv -f "${TOKEN_FILE}.tmp" "$TOKEN_FILE"
        export LLMPOD_METRICS_TOKEN="$TOKEN"
    fi
}

if [ "$ROLE" = "webserver" ]; then
    echo "[entrypoint] running DB init (idempotent) for role=webserver..."
    python scripts/init_db.py
    echo "[entrypoint] DB init done, starting management service..."
    ensure_metrics_token
elif [ "$ROLE" = "proxyserver" ]; then
    ensure_metrics_token
fi

exec python -m llmpod "$@"
