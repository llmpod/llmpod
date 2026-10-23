#!/usr/bin/env bash
# E2E 测试一键运行：拉起独立测试栈 → 动态探测端口 → 等 webserver 就绪 → 跑 pytest → 必拆栈。
# 由 `make test-e2e` 调用，也可直接 `bash tests/e2e/run.sh`。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT/tests/e2e/docker-compose.test.yml"
cd "$ROOT"

# 为所有 compose 子命令提供安全默认值：compose 对整个文件做插值，${LLMPOD_E2E_PORT:?}
# 会让 down/logs 等子命令在没有该变量时也失败。这里在 wrapper 层兜底 :-0，
# up 的真实端口绑定仍由命令级显式 LLMPOD_E2E_PORT="$CANDIDATE" 覆盖（命令级 > 函数级）。
dc() { LLMPOD_E2E_PORT="${LLMPOD_E2E_PORT:-0}" docker compose -f "$COMPOSE_FILE" "$@"; }

# 1. 确保镜像与源码同步：镜像缺失或源码比镜像新则（重新）构建。
#    否则改了 llmpod/ 源码但镜像没重建时，测试栈会悄悄跑老代码（假绿/行为漂移）。
need_rebuild() {
  # 镜像不存在 → 必须构建
  docker image inspect llmpod:latest >/dev/null 2>&1 || return 0
  local img_created src_newest
  img_created=$(docker image inspect llmpod:latest --format '{{.Created}}')
  # 取源码（含打包定义）中最新修改时间（epoch 秒）
  src_newest=$(find llmpod scripts docker pyproject.toml uv.lock -type f -printf '%T@\n' 2>/dev/null \
    | sort -rn | head -1 | cut -d. -f1)
  # 没有任何源码文件（异常情形）→ 不重建
  [ -n "$src_newest" ] || return 1
  # 源码最新 mtime > 镜像创建时间 → 重建
  python3 - "$img_created" "$src_newest" <<'PY'
import sys, datetime
img = sys.argv[1].replace("Z", "+00:00")  # docker .Created 是 ISO8601 + 'Z' 后缀
src = int(sys.argv[2])
img_epoch = int(datetime.datetime.fromisoformat(img).timestamp())
sys.exit(0 if src > img_epoch else 1)  # 0 = 需要 rebuild
PY
}
if need_rebuild; then
  echo "[run.sh] 镜像缺失或源码比镜像新，构建 llmpod:latest..."
  docker build -f docker/Dockerfile -t llmpod:latest .
fi

# 2. 动态探测空闲端口 + 冲突重试
pick_free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); p=s.getsockname()[1]; s.close(); print(p)'
}

PORT=""
for attempt in $(seq 1 5); do
  CANDIDATE="$(pick_free_port)"
  echo "[run.sh] 尝试端口 $CANDIDATE（第 $attempt/5 次）"
  UPLOG="$(mktemp)"
  if LLMPOD_E2E_PORT="$CANDIDATE" dc up -d >"$UPLOG" 2>&1; then
    PORT="$CANDIDATE"
    echo "[run.sh] 栈已启动，webserver -> http://localhost:$PORT"
    break
  fi
  if grep -qiE "address already in use|port is already allocated" "$UPLOG"; then
    echo "[run.sh] 端口 $CANDIDATE 冲突，换一个重试"
    dc down --remove-orphans >/dev/null 2>&1 || true
    continue
  fi
  echo "[run.sh] compose up 失败（非端口冲突），日志："
  cat "$UPLOG"
  exit 1
done

# 探测循环每轮 mktemp 一个 UPLOG；最后一轮的遗留文件在此清理，避免泄漏。
rm -f "$UPLOG" 2>/dev/null || true

if [ -z "$PORT" ]; then
  echo "[run.sh] 5 次探测端口均失败，放弃"
  exit 1
fi

# 3. 必拆栈（无论成败）
cleanup() {
  echo "[run.sh] 拆栈并清理卷..."
  dc down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 4. 等 webserver 健康（最多 ~120s）
BASE_URL="http://localhost:$PORT"
echo "[run.sh] 等待 $BASE_URL/health 就绪..."
for i in $(seq 1 60); do
  if curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
    echo "[run.sh] webserver 就绪"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "[run.sh] webserver 健康检查超时，最近日志："
    dc logs --tail=50 webserver || true
    exit 1
  fi
  sleep 2
done

# 5. 跑 pytest（透传退出码）
echo "[run.sh] 运行 E2E 测试..."
LLMPOD_E2E=1 \
LLMPOD_E2E_BASE_URL="$BASE_URL" \
LLMPOD_E2E_PORT="$PORT" \
uv run pytest tests/e2e -v
