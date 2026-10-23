"""节点引导相关公开端点（无需登录）。

仅放真正公开、供目标 GPU 机器 `curl ... | bash` 一键拉起 Node 的端点。
端点免登录，但验短期签名凭据；普通引导命令可供多台新机器使用。
引导命令含凭据，应通过 HTTPS 分发并妥善保管。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Query, Response

from llmpod.apps.webserver.app import get_settings
from llmpod.apps.webserver.utils.bootstrap_params import BootstrapNodeParams
from llmpod.apps.webserver.utils.bootstrap_token import verify_bootstrap_token, bootstrap_signing_key
from llmpod.core.advertise import manager_ws_candidates, manager_ws_url
from llmpod.core.exceptions import AuthError, ConfigError, NotFound
from llmpod.db import get_db_context
from llmpod.services.node_service import validate_cluster

log = logging.getLogger("llmpod.apps.webserver.routes.public.node")

router = APIRouter()

# 渲染后残留占位符（docker --format 的 {{.Names}} 含点，不在此列，不会误报）
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{\{[a-z_]+\}\}")


@router.get(
    "/bootstrap",
    response_class=Response,
    summary="获取节点引导脚本",
)
async def get_bootstrap_script(
    cluster_id: int = Query(..., description="集群ID"),
    token: str = Query(..., description="节点引导令牌，通过 POST /admin/nodes/bootstrap-token 获取"),
    node_ip: str | None = Query(None, description="节点互联 IP；无终端安装时必填，交互安装时可在脚本中选择"),
    container_name: str | None = Query(None, description="节点容器名，默认 llmpod-node"),
    data_volume: str | None = Query(None, description="节点数据卷，可填卷名或宿主机绝对路径；默认 llmpod-node-data"),
    model_host_dir: str | None = Query(
        None,
        description="宿主机模型目录；留空使用管理端配置的路径，自定义目录需已存在",
    ),
    extra_mounts: list[str] | None = Query(None, description="额外挂载，可重复填写 宿主路径或卷名:容器路径[:mode]"),
    image: str | None = Query(None, description="节点镜像，默认使用管理端当前版本"),
):
    """渲染 addnode.sh 引导脚本（`curl ... | bash` 一键模式）。

    流程：验签引导令牌（签名 + 过期 + cluster_id 绑定）→ 校验集群存在且 active →
    校验引导参数（字符白名单，值会文本嵌入 bash 脚本）→ 读 ``scripts/addnode_template.sh``
    模板 → 占位符统一渲染 → 返回 ``text/x-shellscript``。引导令牌 / Manager WS
    URL / cluster_id / 启动参数内嵌为环境变量与 docker run 参数，目标机器执行后
    Node 容器即连回 Manager。

    安全：本端点免登录，但必须携带管理员签发的有效引导令牌；令牌在有效期内
    可重复拉取并用于多个新节点。安装参数只是默认值，可在目标机修改。
    """
    settings = get_settings()

    # 1. 验证引导令牌（部署密钥签名 + 过期 + cluster_id 绑定）。
    signing_key = bootstrap_signing_key(settings)
    if not verify_bootstrap_token(signing_key, token, cluster_id):
        raise AuthError("节点引导令牌无效或已过期")

    # 3. 校验集群存在且活跃（复用 node_service.validate_cluster）
    async with get_db_context() as db:
        if not await validate_cluster(cluster_id, db):
            raise NotFound("Cluster", cluster_id)

    # 4. 引导参数校验 + 填默认（canonical 容器内路径恒取 manager 的 model_store_dir，
    #    集群全节点一致）。校验放在鉴权之后：未认证者拿 401 而非 422 细节。
    params = BootstrapNodeParams.from_raw(
        node_ip=node_ip,
        container_name=container_name,
        data_volume=data_volume,
        model_host_dir=model_host_dir,
        extra_mounts=extra_mounts or [],
        image=image,
    ).resolved(settings.model_store_dir)
    node_token = settings.node_token
    if not node_token:
        token_path = Path(settings.node_token_file)
        node_token = token_path.read_text().strip() if token_path.exists() else ""
    if not node_token:
        raise ConfigError("节点共享令牌未就绪")

    # 5. 读取引导脚本模板（仓库内 scripts/addnode_template.sh，CWD 相对）
    template_path = Path("scripts/addnode_template.sh")
    if not template_path.exists():
        raise ConfigError(f"引导脚本模板缺失: {template_path}")
    template = template_path.read_text()

    # 6. 渲染占位符（单轮循环：值内不含其它占位符，charset 已禁 {}）。
    #    manager_ws_url 为「节点连 Manager 的 WS 地址」，由 LLMPOD_NODE_MANAGER_IP（IP/域名）
    #    经 core.advertise 拼出，不能沿用绑定地址 0.0.0.0——与文件服务端点、引导命令同源。
    #    manager_ws_candidates 是它的多地址版（内网直连候选在前、主地址兜底压轴），
    #    渲染成单引号空格串直接落进 bash 数组，节点机逐个探活自选回连路径。
    replacements = {
        "node_token": node_token,
        "bootstrap_token": token,
        "manager_ws_url": manager_ws_url(),
        "manager_ws_candidates": " ".join(f"'{url}'" for url in manager_ws_candidates()),
        "cluster_id": str(cluster_id),
        **params.render_items(),
    }
    script = template
    for key, value in replacements.items():
        script = script.replace("{{" + key + "}}", value)

    # 模板/代码漂移兜底：有占位符没被渲染说明模板新增了字段而本端点未跟上
    if _UNRESOLVED_PLACEHOLDER_RE.search(script):
        raise ConfigError("引导脚本模板存在未渲染的占位符")

    return Response(content=script, media_type="text/x-shellscript")
