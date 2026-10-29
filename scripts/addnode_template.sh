#!/bin/bash
# 管理端渲染此模板；安装命令由引导令牌接口返回。

set -e

NODE_TOKEN="{{node_token}}"
BOOTSTRAP_TOKEN="{{bootstrap_token}}"
MANAGER_URL="{{manager_ws_url}}"
MANAGER_CANDIDATES=( {{manager_ws_candidates}} )
CLUSTER_ID="{{cluster_id}}"
NODE_NAME="${LLMPOD_NODE_NAME:-$(hostname)}"
IMAGE="${LLMPOD_NODE_IMAGE:-{{image}}}"
CONTAINER_NAME="${LLMPOD_NODE_CONTAINER_NAME:-{{container_name}}}"
DATA_VOLUME="${LLMPOD_NODE_DATA_VOLUME:-{{data_volume}}}"
# node_ip 参数优先于目标机环境变量，避免旧环境变量覆盖新地址。
NODE_IP="{{node_ip}}"
if [ -z "$NODE_IP" ]; then
    NODE_IP="${LLMPOD_NODE_ADVERTISE_IP:-}"
fi
# 容器内模型路径由 manager 确定；宿主路径可单独指定。
MODEL_DIR="{{model_dir}}"
MODEL_HOST_DIR="${LLMPOD_MODEL_HOST_DIR:-{{model_host_dir}}}"
EXTRA_MOUNTS_SPEC="{{extra_mounts}}"

prompt_default() {
    local label="$1" name="$2" answer value="${!2}"
    IFS= read -r -p "$label（默认值: ${value:-留空}，回车确认）: " -t 120 -u 3 answer || answer=""
    if [ -n "$answer" ]; then printf -v "$name" '%s' "$answer"; fi
}

valid_host_path() {
    local value="$1"
    if [[ "$value" == /* ]]; then
        [[ "$value" != / && ${#value} -le 256 ]] && valid_abs_path "$value"
    else
        [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
    fi
}

valid_abs_path() {
    [[ "$1" =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?$ && "/$1/" != *"/../"* ]]
}

valid_mount() {
    local src dst mode extra
    IFS=: read -r src dst mode extra <<< "$1"
    valid_host_path "$src" && valid_abs_path "$dst" && [ -z "$extra" ] \
        && { [ -z "$mode" ] || [[ "$mode" =~ ^[A-Za-z][A-Za-z,]{0,15}$ ]]; }
}

if [ -z "${LLMPOD_BOOTSTRAP_ASSUME_YES:-}" ] && { [ -t 1 ] && { exec 3<>/dev/tty; } 2>/dev/null; }; then
    echo "确认安装参数；直接回车使用当前默认值。"
    prompt_default "节点名称" NODE_NAME
    prompt_default "节点互联 IP" NODE_IP
    prompt_default "节点镜像" IMAGE
    prompt_default "容器名" CONTAINER_NAME
    prompt_default "数据卷" DATA_VOLUME
    prompt_default "宿主机模型目录" MODEL_HOST_DIR
    prompt_default "额外挂载（空格分隔）" EXTRA_MOUNTS_SPEC
fi

if ! [[ "$NODE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$ ]]; then
    echo "错误: 节点名称格式无效，须以字母或数字开头，且仅含字母、数字、点、下划线或连字符（最多 255 字符）。"
    exit 1
fi
if ! [[ "$IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$ ]] \
    || ! [[ "$CONTAINER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || ! valid_host_path "$DATA_VOLUME" || ! valid_abs_path "$MODEL_HOST_DIR"; then
    echo "错误: 安装参数格式无效。"
    exit 1
fi
if [ -n "$EXTRA_MOUNTS_SPEC" ]; then
    read -r -a _mounts_to_validate <<< "$EXTRA_MOUNTS_SPEC"
    if [ "${#_mounts_to_validate[@]}" -gt 16 ]; then
        echo "错误: 额外挂载不能超过 16 项。"
        exit 1
    fi
    for _mount_to_validate in "${_mounts_to_validate[@]}"; do
        if ! valid_mount "$_mount_to_validate"; then
            echo "错误: 额外挂载格式无效: $_mount_to_validate"
            exit 1
        fi
    done
fi

echo "LLMPod 节点引导"
echo "集群 $CLUSTER_ID · 节点 $NODE_NAME · 容器 $CONTAINER_NAME"
echo "镜像 $IMAGE"
echo ""

# Docker 安装流程与站点安装脚本保持一致。
install_docker_if_missing() {
    command -v docker > /dev/null 2>&1 && return 0
    echo "未检测到 Docker。"

    if [ "${LLMPOD_SKIP_DOCKER_INSTALL:-no}" = "yes" ]; then
        echo "请先安装 Docker，再重跑引导命令。"
        return 1
    fi
    if [ -z "${LLMPOD_BOOTSTRAP_ASSUME_YES:-}" ]; then
        # curl | bash 的 stdin 是脚本内容，只从终端读确认。
        if ! { [ -t 1 ] && { exec 3<>/dev/tty; } 2>/dev/null; }; then
            echo "错误: 无法确认安装。请手动安装 Docker，或设置 LLMPOD_BOOTSTRAP_ASSUME_YES=1。"
            return 1
        fi
        local answer
        IFS= read -r -p "安装 Docker？[y/N]: " -t 60 -u 3 answer || answer=""
        case "$answer" in
            y|Y|yes|YES) ;;
            *) echo "已取消。请先安装 Docker 后重试。"; return 1 ;;
        esac
    fi
    if [ "$(id -u 2>/dev/null)" != "0" ]; then
        echo "错误: 自动安装 Docker 需要 root，请在引导命令前加 sudo。"
        return 1
    fi
    if ! command -v curl > /dev/null 2>&1; then
        echo "错误: 找不到 curl，无法下载安装脚本。"
        return 1
    fi

    # 安装脚本的部分下载路径依赖 wget。
    if ! command -v wget > /dev/null 2>&1; then
        if command -v yum > /dev/null 2>&1; then
            yum install -y wget || echo "警告: wget 安装失败，Docker 安装可能受影响。"
        elif command -v apt-get > /dev/null 2>&1; then
            apt-get install -y wget || echo "警告: wget 安装失败，Docker 安装可能受影响。"
        fi
    fi

    local installer
    installer="$(mktemp /tmp/llmpod-docker.XXXXXX)" || return 1
    if ! curl -fsSL --retry 3 --retry-delay 2 --max-time 30 \
        https://download.bt.cn/install/0/docker_install.sh -o "$installer"; then
        rm -f "$installer"
        echo "错误: Docker 安装脚本下载失败。"
        return 1
    fi
    echo "安装 Docker..."
    bash "$installer" install mirrors.aliyun.com/docker-ce \
        || echo "警告: 安装脚本返回错误，检查 Docker 是否可用。"
    rm -f "$installer"
    if ! command -v docker > /dev/null 2>&1; then
        echo "错误: Docker 安装未完成，请查看上方输出。"
        return 1
    fi
    if command -v systemctl > /dev/null 2>&1; then
        systemctl enable --now docker 2>/dev/null || true
    fi
}

install_docker_if_missing || exit 1
if ! docker info > /dev/null 2>&1; then
    echo "错误: Docker 服务不可用，或当前用户无权访问。"
    exit 1
fi

bootstrap_confirm_or_exit() {
    local title="$1"; shift
    echo "警告: $title"
    local line
    for line in "$@"; do echo "  $line"; done
    # 只屏蔽打开 tty 的报错，保留后续命令的 stderr。
    if [ -n "${LLMPOD_BOOTSTRAP_ASSUME_YES:-}" ] || ! { [ -t 1 ] && { exec 3<>/dev/tty; } 2>/dev/null; }; then
        echo "  无法交互确认。修复后重试。"
        exit 1
    fi
    local ans
    IFS= read -r -p "  输入 c 跳过该设备，回车退出: " -t 60 -u 3 ans || ans=""
    if [ "$ans" != "c" ] && [ "$ans" != "C" ]; then
        echo "已退出。修复后重试。"
        exit 1
    fi
    echo "已跳过该设备。"
}

# 只识别显示设备，避免将 NVIDIA 网卡误判为 GPU。
NVIDIA_HW_PRESENT=0
if command -v lspci &> /dev/null \
   && lspci 2>/dev/null | grep -Eqi '(vga compatible controller|3d controller|display controller).*nvidia'; then
    NVIDIA_HW_PRESENT=1
fi

# 驱动不可用时，确认是否跳过 NVIDIA。
NVIDIA_DRIVER_OK=0
GPU_SKIP_REASON=""
if command -v nvidia-smi &> /dev/null; then
    if nvidia-smi -L &> /dev/null; then
        NVIDIA_DRIVER_OK=1
    else
        bootstrap_confirm_or_exit \
            "NVIDIA 驱动不可用（nvidia-smi -L 失败）。" \
            "请检查驱动。"
        GPU_SKIP_REASON="NVIDIA 驱动不可用"
    fi
elif [ "$NVIDIA_HW_PRESENT" = "1" ]; then
    bootstrap_confirm_or_exit \
        "检测到 NVIDIA GPU，但找不到 nvidia-smi。" \
        "请安装驱动。"
    GPU_SKIP_REASON="NVIDIA 驱动未安装"
fi

# NVIDIA 官方仓库：仅在驱动正常、Docker 缺少 nvidia 运行时时安装。
install_nvidia_toolkit() {
    if command -v nvidia-ctk > /dev/null 2>&1; then return 0; fi
    if command -v apt-get > /dev/null 2>&1; then
        apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg2 || return 1
        if [ ! -s /etc/apt/sources.list.d/nvidia-container-toolkit.list ]; then
            local repo_tmp
            repo_tmp="$(mktemp -d /tmp/llmpod-nvidia.XXXXXX)" || return 1
            if ! curl -fsSL --retry 3 --max-time 30 https://nvidia.github.io/libnvidia-container/gpgkey -o "$repo_tmp/key" \
                || ! curl -fsSL --retry 3 --max-time 30 https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o "$repo_tmp/list" \
                || ! gpg --dearmor -o "$repo_tmp/keyring.gpg" "$repo_tmp/key" \
                || ! sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
                    "$repo_tmp/list" > "$repo_tmp/repo.list" \
                || ! install -m 0644 "$repo_tmp/keyring.gpg" /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
                || ! install -m 0644 "$repo_tmp/repo.list" /etc/apt/sources.list.d/nvidia-container-toolkit.list; then
                rm -rf "$repo_tmp"
                return 1
            fi
            rm -rf "$repo_tmp"
        fi
        apt-get update && apt-get install -y nvidia-container-toolkit || return 1
    elif command -v dnf > /dev/null 2>&1 || command -v yum > /dev/null 2>&1; then
        local pkg=dnf repo_tmp
        command -v dnf > /dev/null 2>&1 || pkg=yum
        if [ ! -s /etc/yum.repos.d/nvidia-container-toolkit.repo ]; then
            repo_tmp="$(mktemp /tmp/llmpod-nvidia-repo.XXXXXX)" || return 1
            if ! curl -fsSL --retry 3 --max-time 30 https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo -o "$repo_tmp" \
                || ! install -m 0644 "$repo_tmp" /etc/yum.repos.d/nvidia-container-toolkit.repo; then
                rm -f "$repo_tmp"
                return 1
            fi
            rm -f "$repo_tmp"
        fi
        "$pkg" install -y nvidia-container-toolkit || return 1
    else
        echo "错误: 当前系统没有 apt、dnf 或 yum，请手动安装 NVIDIA Container Toolkit。"
        return 1
    fi
    command -v nvidia-ctk > /dev/null 2>&1
}

nvidia_runtime_available() {
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'
}

prepare_nvidia_runtime() {
    nvidia_runtime_available && return 0
    if [ "${LLMPOD_SKIP_NVIDIA_TOOLKIT_INSTALL:-no}" = "yes" ]; then
        echo "错误: Docker 缺少 nvidia 运行时，请先安装 NVIDIA Container Toolkit。"
        exit 1
    fi

    if [ -z "${LLMPOD_BOOTSTRAP_ASSUME_YES:-}" ]; then
        if ! { [ -t 1 ] && { exec 3<>/dev/tty; } 2>/dev/null; }; then
            echo "错误: Docker 缺少 nvidia 运行时，无法确认安装 Toolkit。"
            exit 1
        fi
        local answer
        IFS= read -r -p "安装 NVIDIA Container Toolkit 并重启 Docker？[y/N/c=跳过 GPU]: " -t 60 -u 3 answer || answer=""
        case "$answer" in
            y|Y|yes|YES) ;;
            c|C) return 1 ;;
            *) echo "已退出。"; exit 1 ;;
        esac
    fi
    if [ "$(id -u 2>/dev/null)" != "0" ]; then
        echo "错误: 安装 Toolkit 需要 root，请在引导命令前加 sudo。"
        exit 1
    fi
    if ! command -v systemctl > /dev/null 2>&1; then
        echo "错误: 找不到 systemctl，无法自动重启 Docker。"
        exit 1
    fi

    echo "配置 NVIDIA Container Toolkit..."
    if ! install_nvidia_toolkit; then
        echo "错误: Toolkit 安装失败，请查看上方输出。"
        exit 1
    fi
    local config="${1:-/etc/docker/daemon.json}" backup="" had_config=0
    if [ -f "$config" ]; then
        backup="$(mktemp /tmp/llmpod-docker-config.XXXXXX)" || exit 1
        cp -p "$config" "$backup" || { rm -f "$backup"; exit 1; }
        had_config=1
    fi
    if ! nvidia-ctk runtime configure --runtime=docker --config="$config" \
        || ! systemctl restart docker \
        || ! docker info > /dev/null 2>&1 \
        || ! nvidia_runtime_available; then
        if [ "$had_config" = "1" ]; then
            cp -p "$backup" "$config" || { echo "错误: Docker 配置恢复失败，备份在 $backup"; exit 1; }
        else
            rm -f "$config" || { echo "错误: 无法删除失败的 Docker 配置: $config"; exit 1; }
        fi
        systemctl restart docker 2>/dev/null || true
        rm -f "$backup"
        echo "错误: NVIDIA 运行时配置失败，已恢复 Docker 配置。"
        exit 1
    fi
    rm -f "$backup"
}

if [ "$NVIDIA_DRIVER_OK" = "1" ] && ! prepare_nvidia_runtime; then
    NVIDIA_DRIVER_OK=0
    GPU_SKIP_REASON="已跳过 NVIDIA Container Toolkit"
fi

# 缺少设备节点时，确认是否跳过 Ascend。
ASCEND_SKIP_REASON=""
if command -v npu-smi &> /dev/null && ! ls /dev/davinci[0-9]* > /dev/null 2>&1; then
    bootstrap_confirm_or_exit \
        "找不到 Ascend 设备节点 /dev/davinci*。" \
        "请检查驱动服务。"
    ASCEND_SKIP_REASON="Ascend 驱动未就绪"
fi

# 按候选顺序检查 /health，避免误连同网段的其他服务。
# 候选是完整 WS 回连地址（带固定路径 /ws/node），健康路由在根 /health——
# 必须剥掉路径再拼，否则拼出 /ws/node/health 恒 404，探测形同虚设。
probe_health() {
    local u="${1/#ws/http}"
    u="${u#*://}"
    local origin="${u%%/*}"
    curl -fsS --max-time 2 -o /dev/null "http://${origin}/health" 2>/dev/null
}

if command -v curl > /dev/null 2>&1; then
    CHOSEN_URL=""
    for c in "${MANAGER_CANDIDATES[@]}"; do
        if probe_health "$c"; then
            CHOSEN_URL="$c"
            break
        fi
    done
    if [ -n "$CHOSEN_URL" ]; then
        MANAGER_URL="$CHOSEN_URL"
    else
        echo "警告: 管理端地址未通过探测，尝试 $MANAGER_URL"
    fi
else
    echo "警告: 找不到 curl，使用 $MANAGER_URL"
fi
echo "管理端: $MANAGER_URL"

# 设备与容器参数。
RUN_FLAGS=()
ENV_FLAGS=()
DEVICE_SUMMARY="CPU-only（未检测到加速设备）"

if [ "$NVIDIA_DRIVER_OK" = "1" ]; then
    echo "NVIDIA GPU:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
    RUN_FLAGS+=(--runtime=nvidia --gpus all)
    DEVICE_SUMMARY="NVIDIA"
elif [ -n "$GPU_SKIP_REASON" ]; then
    DEVICE_SUMMARY="CPU-only（$GPU_SKIP_REASON）"
fi

# Ascend 需要设备节点、驱动目录和 npu-smi 一起挂载。
DAVINCI_DEVS=( $(ls /dev/davinci[0-9]* 2>/dev/null || true) )
if [ ${#DAVINCI_DEVS[@]} -gt 0 ]; then
    echo "检测到 Ascend NPU: ${DAVINCI_DEVS[*]}"
    for dev in "${DAVINCI_DEVS[@]}" /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc; do
        RUN_FLAGS+=(--device "$dev:$dev:rwm")
    done
    RUN_FLAGS+=(
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi:ro
        -v /usr/local/dcmi:/usr/local/dcmi:ro
        -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro
        -v /etc/ascend_install.info:/etc/ascend_install.info:ro
    )
    ENV_FLAGS+=(
        -e "LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/common/lib:/usr/local/Ascend/driver/lib64/driver:/usr/local/dcmi/lib64"
    )
    if [ "$DEVICE_SUMMARY" = "NVIDIA" ]; then
        DEVICE_SUMMARY="NVIDIA + Ascend"
    else
        DEVICE_SUMMARY="Ascend"
    fi
elif [ -n "$ASCEND_SKIP_REASON" ] && [ "$DEVICE_SUMMARY" = "CPU-only（未检测到加速设备）" ]; then
    DEVICE_SUMMARY="CPU-only（$ASCEND_SKIP_REASON）"
fi

# 互联 IP 由参数、环境变量或菜单指定；自动化安装必须传入地址。
# 虚拟接口前缀与 collectors/system.py 保持一致。
VIRTUAL_IFACE_PREFIXES="docker br- veth virbr kube cni flannel calico tunl cbr0"
# Docker 自建网卡（docker0 / br-<id> / veth*）不进候选清单：管理端跑在容器里，
# 这些地址即便同机也多半不可达，列出来只会让人配一个打不通的互联 IP。
HIDDEN_IFACE_PREFIXES="docker br- veth"

iface_has_prefix() {
    local prefixes="$1" iface="$2" p
    for p in $prefixes; do
        case "$iface" in "$p"*) return 0 ;; esac
    done
    return 1
}

valid_ipv4() {
    local ip="$1" o x
    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS='.' read -r -a o <<< "$ip"
    for x in "${o[@]}"; do
        # 前导零在不同解析器中的含义不一致。
        [[ "$x" =~ ^0[0-9]+$ ]] && return 1
        [ "$x" -gt 255 ] && return 1
    done
    # 地址范围须与 node_addr.py 和 bootstrap_params.py 的校验一致。
    [ "${o[0]}" -eq 0 ] && return 1
    [ "${o[0]}" -eq 127 ] && return 1
    [ "${o[0]}" -eq 169 ] && [ "${o[1]}" -eq 254 ] && return 1
    [ "${o[0]}" -ge 224 ] && return 1
    return 0
}

# 网卡地址与虚拟接口分开显示。
REAL_IFACES=(); REAL_IPS=()
VIRT_IFACES=(); VIRT_IPS=()
if command -v ip &> /dev/null; then
    while read -r iface addr; do
        if [ "$iface" = "lo" ] || [ -z "$addr" ] || ! valid_ipv4 "$addr"; then continue; fi
        if iface_has_prefix "$HIDDEN_IFACE_PREFIXES" "$iface"; then continue; fi
        if iface_has_prefix "$VIRTUAL_IFACE_PREFIXES" "$iface"; then
            VIRT_IFACES+=("$iface"); VIRT_IPS+=("$addr")
        else
            REAL_IFACES+=("$iface"); REAL_IPS+=("$addr")
        fi
    done < <(ip -o -4 addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print $2, a[1]}')
else
    # 没有 ip 命令时从 hostname -I 获取地址（拿不到网卡名，无法按前缀过滤）。
    while read -r addr; do
        if [ -n "$addr" ] && valid_ipv4 "$addr"; then REAL_IFACES+=("?"); REAL_IPS+=("$addr"); fi
    done < <(hostname -I 2>/dev/null | tr ' ' '\n')
fi

# 交互菜单。超时不选地址。
menu_select() {
    local labels=("$@")
    local n=$# sel=0 i key rest
    trap 'printf "\033[?25h" > /dev/tty 2>/dev/null || true' EXIT
    _menu_draw() {
        for i in "${!labels[@]}"; do
            if [ "$i" -eq "$sel" ]; then
                printf '  \033[36m❯ %d) %s\033[0m\033[K\n' "$((i + 1))" "${labels[$i]}"
            else
                printf '    %d) %s\033[K\n' "$((i + 1))" "${labels[$i]}"
            fi
        done
    }
    _menu_draw
    while :; do
        IFS= read -rsn1 -u 3 -t 60 key || { MENU_SEL=""; break; }
        case "$key" in
            $'\x1b')
                IFS= read -rsn2 -u 3 -t 0.05 rest || rest=""
                case "$rest" in
                    '[A') sel=$(((sel - 1 + n) % n)) ;;
                    '[B') sel=$(((sel + 1) % n)) ;;
                esac
                ;;
            j|J) sel=$(((sel + 1) % n)) ;;
            k|K) sel=$(((sel - 1 + n) % n)) ;;
            [1-9])
                if [ "$key" -le "$n" ]; then sel=$((key - 1)); MENU_SEL=$sel; break; fi
                ;;
            '') MENU_SEL=$sel; break ;;
        esac
        printf '\033[%dA' "$n"
        _menu_draw
    done
    printf '\033[%dA\033[J\033[?25h' "$n"
}

# IP 必须经过校验；无终端时不自动选择。
die_ip_required() {
    echo "错误: $1"
    echo "请在引导命令中传入 node_ip=<本机互联IP>，或在菜单中选择。"
    exit 1
}
if [ -n "$NODE_IP" ]; then
    if ! valid_ipv4 "$NODE_IP"; then
        echo "错误: 互联 IP $NODE_IP 无效，请检查 node_ip 或 LLMPOD_NODE_ADVERTISE_IP。"
        exit 1
    fi
elif [ -n "${LLMPOD_BOOTSTRAP_ASSUME_YES:-}" ] || ! { [ -t 1 ] && { exec 3<>/dev/tty; } 2>/dev/null; }; then
    die_ip_required "未指定节点互联 IP。"
else
    IP_LABELS=()
    IP_VALUES=()
    for i in "${!REAL_IPS[@]}"; do
        IP_LABELS+=("网卡 ${REAL_IFACES[$i]}  ${REAL_IPS[$i]}")
        IP_VALUES+=("${REAL_IPS[$i]}")
    done
    for i in "${!VIRT_IPS[@]}"; do
        IP_LABELS+=("${VIRT_IFACES[$i]}  ${VIRT_IPS[$i]}  (虚拟接口)")
        IP_VALUES+=("${VIRT_IPS[$i]}")
    done
    IP_LABELS+=("手动输入 IP…")
    IP_VALUES+=("__MANUAL__")

    echo "选择其他节点可访问的本机 IP（方向键、数字或手动输入；回车确认）:"
    menu_select "${IP_LABELS[@]}"
    [ -n "$MENU_SEL" ] || die_ip_required "未选择互联 IP。"
    choice="${IP_VALUES[$MENU_SEL]}"

    if [ "$choice" = "__MANUAL__" ]; then
        while :; do
            IFS= read -r -p "  输入互联 IPv4: " -t 60 -u 3 ip || ip=""
            if [ -n "$ip" ]; then
                if valid_ipv4 "$ip"; then choice="$ip"; break; fi
                echo "  IP 无效: $ip，请重输。"
                continue
            fi
            die_ip_required "未输入互联 IP。"
        done
    fi
    NODE_IP="$choice"
fi
echo "互联 IP: $NODE_IP"

# 每次检查远端更新；离线环境可使用本地镜像。
echo "拉取镜像: $IMAGE"
if ! docker pull "$IMAGE"; then
    if docker image inspect "$IMAGE" > /dev/null 2>&1; then
        echo "警告: 拉取失败，使用本地镜像 $IMAGE。"
    else
        echo "错误: 拉取失败，且本地没有 $IMAGE。请检查镜像源或离线导入："
        echo "  docker pull $IMAGE && docker save $IMAGE | gzip > llmpod-image.tar.gz"
        echo "  拷贝镜像包到本机后运行 gunzip -c llmpod-image.tar.gz | docker load"
        exit 1
    fi
fi

# 使用节点镜像验证 GPU 注入，避免注册了运行时却无法启动容器。
if [ "$NVIDIA_DRIVER_OK" = "1" ]; then
    if ! docker run --rm --runtime=nvidia --gpus all --entrypoint nvidia-smi "$IMAGE" -L > /dev/null; then
        echo "错误: NVIDIA 容器自检失败，请检查驱动和 Container Toolkit。"
        exit 1
    fi
fi

# 推理容器由节点通过宿主 Docker 创建，模型目录必须在宿主机可见。
if [ "$MODEL_HOST_DIR" != "$MODEL_DIR" ]; then
    if [ ! -d "$MODEL_HOST_DIR" ]; then
        echo "错误: 宿主模型目录不存在: $MODEL_HOST_DIR"
        exit 1
    fi
else
    mkdir -p "$MODEL_DIR"
fi
RUN_FLAGS+=(-v "$MODEL_HOST_DIR:$MODEL_DIR")
ENV_FLAGS+=(-e "LLMPOD_MODEL_STORE_DIR=$MODEL_DIR")
ENV_FLAGS+=(-e "LLMPOD_MODEL_HOST_DIR=$MODEL_HOST_DIR")

EXTRA_MOUNTS=()
if [ -n "$EXTRA_MOUNTS_SPEC" ]; then
    read -r -a EXTRA_MOUNTS <<< "$EXTRA_MOUNTS_SPEC"
fi
for m in "${EXTRA_MOUNTS[@]}"; do
    RUN_FLAGS+=(-v "$m")
done

ENV_FLAGS+=(-e "LLMPOD_NODE_ADVERTISE_IP=$NODE_IP")

# 容器名可能含点号，匹配时按固定字符串处理。
if docker ps -a --format '{{.Names}}' | grep -qxF "${CONTAINER_NAME}"; then
    echo "替换容器: $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" > /dev/null
fi

# 节点使用 host 网络；自身容器名和镜像用于后续升级。
echo "启动容器: $CONTAINER_NAME"
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network host \
  --no-healthcheck \
  --label "llmpod.node=true" \
  -e "LLMPOD_NODE_TOKEN=$NODE_TOKEN" \
  -e "LLMPOD_NODE_BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN" \
  -e "LLMPOD_NODE_MANAGER_URL=$MANAGER_URL" \
  -e "LLMPOD_NODE_CLUSTER_ID=$CLUSTER_ID" \
  -e "LLMPOD_NODE_NAME=$NODE_NAME" \
  -e "LLMPOD_NODE_CONTAINER_NAME=$CONTAINER_NAME" \
  -e "LLMPOD_NODE_IMAGE=$IMAGE" \
  "${ENV_FLAGS[@]}" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$DATA_VOLUME":/app/data \
  "${RUN_FLAGS[@]}" \
  "$IMAGE" \
  node

echo "容器已启动: $CONTAINER_NAME"
echo "设备: $DEVICE_SUMMARY | 互联 IP: $NODE_IP"
echo "查看日志: docker logs -f $CONTAINER_NAME"
