#!/usr/bin/env bash
# 一键改管理端对外地址（组网切换用）：自动探测本机 IPv4，写入 .env / docker/.env。
#
# 三个键一起改，避免手改漏项——它们必须同口径，改一个漏一个的症状是
# 「引导出来的节点回连地址打不通」：
#   LLMPOD_NODE_MANAGER_IP                主地址（tailscale 优先，无则取内网）
#   LLMPOD_NODE_MANAGER_IP_LAN            内网直连候选（引导脚本给节点逐个探活优选）
#   LLMPOD_MULTINODE_SAME_SUBNET_PREFIX   多机同子网校验（按主地址自动定 10 / 24）
#
# 用法:
#   scripts/set_manager_addr.sh                    # 自动探测
#   scripts/set_manager_addr.sh 100.88.220.116     # 指定主地址
#   RESTART=1 scripts/set_manager_addr.sh          # 改完顺带重启管理端
#
# 改完必须重启管理端才生效（settings 启动期从 .env 载入，--reload 只盯 .py）。
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILES=(.env docker/.env)
PRIMARY="${1:-}"

# 与 scripts/addnode_template.sh 的 valid_ipv4 / VIRTUAL_IFACE_PREFIXES 同口径：
# 两侧放行的值必须一致，否则脚本写进去的地址引导脚本不认。
valid_ipv4() {   # $1=候选串；可作互联地址的 IPv4 单播返回 0
    local ip="$1" o x
    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS='.' read -r -a o <<< "$ip"
    for x in "${o[@]}"; do
        [[ "$x" =~ ^0[0-9]+$ ]] && return 1
        [ "$x" -gt 255 ] && return 1
    done
    [ "${o[0]}" -eq 0 ] && return 1
    [ "${o[0]}" -eq 127 ] && return 1
    [ "${o[0]}" -eq 169 ] && [ "${o[1]}" -eq 254 ] && return 1
    [ "${o[0]}" -ge 224 ] && return 1
    return 0
}

is_virtual_iface() {   # 容器/网桥地址别的机器不可达，不进候选
    case "$1" in
        docker*|br-*|veth*|virbr*|kube*|cni*|flannel*|calico*|cbr0*) return 0 ;;
    esac
    return 1
}

# 枚举本机全局 IPv4：tailscale 单列（组网主用），真实网卡进内网候选。
# 用 ip -o -4 的输出格式取字段（与 addnode 模板同一处口径）。
TS_ADDRS=()
REAL_ADDRS=()
while read -r iface addr; do
    [ "$iface" = "lo" ] && continue
    [ -z "$addr" ] && continue
    valid_ipv4 "$addr" || continue
    if [ "${iface#tailscale}" != "$iface" ]; then
        TS_ADDRS+=("$addr")
    elif ! is_virtual_iface "$iface"; then
        REAL_ADDRS+=("$addr")
    fi
done < <(ip -o -4 addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print $2, a[1]}')

if [ -z "$PRIMARY" ]; then
    # tailscale 优先：组网机器在外面，默认让它们连得进来；没 tailscale 才用内网
    if [ "${#TS_ADDRS[@]}" -gt 0 ]; then
        PRIMARY="${TS_ADDRS[0]}"
    elif [ "${#REAL_ADDRS[@]}" -gt 0 ]; then
        PRIMARY="${REAL_ADDRS[0]}"
    fi
fi
if [ -z "$PRIMARY" ] || ! valid_ipv4 "$PRIMARY"; then
    echo "错误: 探测不到可用 IPv4（或传入的「$PRIMARY」不是合法单播地址）" >&2
    echo "      可用: tailscale=[${TS_ADDRS[*]:-无}] 内网=[${REAL_ADDRS[*]:-无}]" >&2
    exit 1
fi

# 内网直连候选 = 真实网卡地址去掉主地址；节点机上逐个探活（GET /health），
# 内网通的走直连，全不通再落主地址——同机 LAN 节点与 tailnet 节点各取所需。
LAN_LIST=""
for addr in "${REAL_ADDRS[@]}"; do
    [ "$addr" = "$PRIMARY" ] && continue
    LAN_LIST="${LAN_LIST:+$LAN_LIST,}$addr"
done

# 同子网校验：tailscale 是 100.64.0.0/10 的散段（每台一个 /32），按 /24 判必然
# 误伤成「跨子网」拦下多机部署；主地址落在此段时放宽到 /10。
IFS='.' read -r o1 o2 _ _ <<< "$PRIMARY"
if [ "$o1" -eq 100 ] && [ "$o2" -ge 64 ] && [ "$o2" -le 127 ]; then
    PREFIX=10
else
    PREFIX=24
fi

set_key() {   # $1=文件 $2=键 $3=值；有则替换、无则追加
    local f="$1" k="$2" v="$3" cur
    [ -f "$f" ] || { echo "  跳过 $f（不存在）"; return 0; }
    # 判「键在不在」与「值空不空」分开：内网候选可以为空串，键必须还在原位
    # 被替换——按值判空会追加出第二个同名键（后面那个取胜，读首条的消费方拿到旧值）
    if grep -q "^${k}=" "$f"; then
        cur=$(sed -n "s/^${k}=//p" "$f" | head -1)
        sed -i "s|^${k}=.*|${k}=${v}|" "$f"
    else
        cur=""
        printf '%s=%s\n' "$k" "$v" >> "$f"
    fi
    printf '  %-38s %s → %s\n' "$k" "${cur:-（未设）}" "$v"
}

echo "写入:"
for f in "${ENV_FILES[@]}"; do
    echo "[$f]"
    set_key "$f" LLMPOD_NODE_MANAGER_IP "$PRIMARY"
    set_key "$f" LLMPOD_NODE_MANAGER_IP_LAN "$LAN_LIST"
    set_key "$f" LLMPOD_MULTINODE_SAME_SUBNET_PREFIX "$PREFIX"
done

PORT=$(sed -n 's/^LLMPOD_WEBSERVER_PORT=//p' .env | head -1)
PORT="${PORT:-8080}"
echo ""
echo "主地址   : $PRIMARY"
echo "内网候选 : ${LAN_LIST:-（无）}"
echo "同子网校验 : /$PREFIX"
echo "回连地址 : ws://$PRIMARY:$PORT/ws/node"

if [ -z "${RESTART:-}" ]; then
    echo ""
    echo "未重启，新地址尚不生效（settings 启动期从 .env 载入）。"
    echo "重启管理端后 addnode 引导命令才会带新地址：make dev（或本脚本 RESTART=1）"
    exit 0
fi

echo ""
echo "重启管理端（:$PORT）..."
pkill -f 'uvicorn llmpod\.app:app' 2>/dev/null || true
for _ in $(seq 1 40); do
    ss -ltn 2>/dev/null | grep -q ":${PORT} " || break
    sleep 0.5
done
mkdir -p .logs
nohup uv run uvicorn llmpod.app:app --host 0.0.0.0 --port "$PORT" --reload \
    >> .logs/control-plane.log 2>&1 &
disown 2>/dev/null || true
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/health" 2>/dev/null; then
        echo "管理端已就绪（日志 .logs/control-plane.log，停用 pkill -f 'uvicorn llmpod.app:app'）"
        exit 0
    fi
    sleep 1
done
echo "错误: 60s 内未起来，看 .logs/control-plane.log" >&2
exit 1
