"""HuggingFace Hub 模型源探测器（manager 侧 httpx，**不下载** 权重）。

流程：
1. ``GET {endpoint}/api/models/{repo}?blobs=true`` → 文件清单(siblings) + sha + pipeline_tag；
2. 抓 config：transformers ``config.json`` 优先，否则 diffusers ``model_index.json``；
3. 纯函数装配 ``ModelFiles``：文件结构事实 + 身份标记 + ``suggested_type``（来自 pipeline_tag）。
4. 任一步失败 → 软失败（``success=False``），已能拿到的部分仍返回。

HF endpoint 可配置（``LLMPOD_MODEL_SOURCE_HF_ENDPOINT``）：国内默认不可达，改 hf-mirror.com。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from llmpod.apps.manager.model_source import inference
from llmpod.apps.manager.model_source import remote_gguf
from llmpod.apps.manager.model_source.base import ModelFiles, ModelInspector
from llmpod.config.settings import settings

log = logging.getLogger("llmpod.apps.manager.model_source.huggingface")


def parse_hf_ref(source_url: str) -> tuple[str, str | None, str | None]:
    """解析 source_url → ``(repo_id, endpoint, subpath)``。

    接受 repo id（``Qwen/Qwen2.5-0.5B``，``endpoint=None`` → 用 ``settings`` 配置的 endpoint）
    或完整 URL（``https://[host]/ns/name[/tree|resolve|blob|commit]/<rev>/<sub...>``，
    ``endpoint`` = 该 URL 的 ``scheme://host``，**优先于 settings**——用户贴 hf-mirror.com
    等镜像 URL 时直接用，不必改全局配置）。

    - ``revision`` **不自动识别**（仅显式入参，默认 ``main``）——URL 里的 ``/tree/v1.0`` 被忽略，
      要指定版本请显式传 ``revision``。
    - ``subpath``：URL 里 verb+rev 之后的路径段（``.../tree/main/Q4_K_M/x.gguf`` → ``Q4_K_M/x.gguf``）。
      用于 GGUF 多变体 repo —— 用户贴变体路径时，inspect 只算该路径下文件。bare URL/repo id → None。
    """
    s = source_url.strip()
    if s.startswith("http://") or s.startswith("https://"):
        parsed = urlparse(s)
        path = parsed.path.strip("/").split("/")
        # path 形如 [ns, name, (tree|resolve|blob|commit)?, <rev>?, <sub...>]
        if len(path) < 2:
            return s, None, None
        repo_id = f"{path[0]}/{path[1]}"
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
        subpath = None
        if len(path) >= 4 and path[2] in ("tree", "resolve", "blob", "commit"):
            # path[3]=rev（忽略），path[4:]=subpath
            tail = [p for p in path[4:] if p]
            subpath = "/".join(tail) or None
        return repo_id, endpoint, subpath
    # 非 URL：当作 repo id，endpoint=None（用 settings）
    return s.split("?")[0], None, None


def build_model_files(
    siblings: list,
    pipeline_tag: str | None,
    *,
    config: dict | None = None,
    model_index: dict | None = None,
    revision: str | None = None,
    sha: str | None = None,
    config_error: str | None = None,
    gguf_is_moe: bool | None = None,
    gguf_header=None,
    source_subpath: str | None = None,
    repo_names: list[str] | None = None,
) -> ModelFiles:
    """从文件清单 + pipeline_tag + （可选）config/model_index 装配识别结果（纯函数，无 IO）。

    - 提取：structure/format/layout/has_config/size_gb/quantization/is_moe/身份原样字符串；
    - type 建议：``suggested_type`` 仅来自 ``pipeline_tag``（映射不了为 None，用户选），**不做 config 语义猜测**；
    - ``gguf_is_moe``：远程 GGUF 头部 metadata 探出的 MoE 判定（GGUF 无 config，靠文件头 KV 判）。
      None=未探/非 GGUF，走 config 推断兜底；``gguf_header``（完整头部元数据）存在时优先，
      同时提供 MoE 与架构形状（层数/KV head/head_dim）。
    - ``repo_names``：仓库**全量**文件清单（未按 subpath 收窄）。视觉投影器（mmproj）常与变体文件
      不同名/不同目录——用户贴具体变体文件时 ``siblings`` 已收窄到那一个文件，用全量清单才能找到它。
    """
    config = config or {}
    names = inference._sibling_names(siblings)
    # 视觉投影器/视觉判定按「仓库全量」探（变体收窄时 mmproj 往往不在收窄范围内）
    vision_names = repo_names if repo_names is not None else names
    # 类别建议：pipeline_tag 优先；上游无 tag（如镜像 API 不返回）但仓库带 mmproj 时，
    # 按文件级信号建议 multimodal——这是 GGUF 视觉最可靠的判据，省掉用户手选。
    suggested_type = inference.type_from_pipeline_tag(pipeline_tag)
    if suggested_type is None and inference.has_mmproj(vision_names):
        suggested_type = "multimodal"

    # —— GGUF 多变体 repo 检测：去掉分片后缀后仍有多个变体键 → 无法自动判定，让用户带路径 ——
    # 覆盖两种布局：(a) 多顶层目录各含 .gguf（如 DeepSeek-V4/Q4_K_M/*.gguf）；
    # (b) 根级多个 .gguf 不同量化（如 Qwen-...-Q4_K_M.gguf、-Q8_0.gguf 并列，无分片关系）。
    # 同变体多分片（-00001-of-00002）归一组，不误判。
    gguf_variants = inference.gguf_variant_keys(names)
    if len(gguf_variants) > 1 and not config and not model_index:
        variants = ", ".join(sorted(v.replace(".gguf", "") or v for v in gguf_variants))
        return ModelFiles(
            success=False,
            message=(
                f"多个 GGUF 量化变体（{len(gguf_variants)} 个），无法自动判定大小。"
                f"请在 source_url 带具体变体路径或文件（如 /resolve/main/Qwen-...-Q4_K_M.gguf）后重试。可选变体：{variants}"
            ),
            structure="directory",
            source_kind=None,
            source_subpath=source_subpath,
            format="gguf",
            layout="gguf",
            has_config=False,
            weight_files=None,
            architecture=None,
            pipeline_tag=pipeline_tag,
            suggested_type=suggested_type,
            # 多变体虽 size 判不出（success=False 禁添加），但已探了默认变体的头：
            # is_moe / is_vision 仍如实返回（供前端预览），用户带具体变体重试后 size 也会补上。
            # 与主路径同优先序：完整头部存在时以其为准（本分支无 config，gguf_is_moe
            # 只剩 None/False 两值——唯一调用方从不传它，None 兜 False 即可）
            is_moe=gguf_header.is_moe if gguf_header is not None else bool(gguf_is_moe),
            is_vision=inference.has_mmproj(vision_names),
            mmproj_file=inference.pick_mmproj(vision_names),
            size_gb=None,
            quantization=None,
        )

    source_kind = inference.detect_source_kind(siblings)
    structure = inference.structure_from_source_kind(source_kind)
    layout = inference.detect_layout(names)
    # "有可读 config"：config.json 或 model_index.json 至少取到其一
    has_config = bool(config) or bool(model_index)
    files = [
        n
        for n in names
        if n.lower().endswith(inference.WEIGHT_EXTS) or n.lower() in ("config.json", "model_index.json")
    ]

    architecture = inference.infer_architecture(config, model_index)

    # message：单文件 / 无可用 config / 完整
    if structure == "single_file":
        message: str | None = "单文件模型（如 gguf / onnx），身份信息有限，可人工补全"
    elif not config and not model_index:
        hint = f"（{config_error}）" if config_error else ""
        message = f"未获取到可用 config{hint}，已尽力识别文件结构，type 与其余字段可人工补全"
    else:
        message = None

    fmt = inference.infer_format(names)
    size_gb = inference.sum_weight_gb(siblings)
    # is_moe：config 有查 num_experts；GGUF 无 config 用远程头部 metadata 探出的结果（None 兜底）
    if config:
        is_moe = inference.infer_moe(config)
    elif gguf_header is not None:
        is_moe = gguf_header.is_moe
    else:
        is_moe = gguf_is_moe if gguf_is_moe is not None else False
    # 架构形状（KV 真公式 + TP 整除校验输入）：有 GGUF 头时以它为准（那才是将被部署的产物）
    shape = inference.arch_shape(config, gguf_header, prefer_gguf=gguf_header is not None)
    return ModelFiles(
        success=inference.classify_success(fmt, size_gb),
        message=message,
        structure=structure,
        source_kind=source_kind,
        source_subpath=source_subpath,
        format=fmt,
        layout=layout,
        has_config=has_config,
        weight_files=files or None,
        architecture=architecture,
        pipeline_class=str(model_index.get("_class_name"))
        if isinstance(model_index, dict) and model_index.get("_class_name")
        else None,
        pipeline_tag=pipeline_tag,
        is_moe=is_moe,
        is_vision=inference.has_mmproj(vision_names),
        mmproj_file=inference.pick_mmproj(vision_names),
        quantization=inference.infer_quant(config, names, gguf_header),
        size_gb=size_gb,
        num_layers=shape.num_layers,
        kv_layers=shape.kv_layers,
        num_attention_heads=shape.num_attention_heads,
        num_kv_heads=shape.num_kv_heads,
        head_dim=shape.head_dim,
        max_context=shape.max_context,
        needs_trust_remote_code=inference.needs_trust_remote_code(config or {}),
        revision=revision or sha,
        suggested_type=suggested_type,
    )


class HFHubInspector(ModelInspector):
    """HuggingFace Hub 探测器。"""

    async def inspect(
        self,
        *,
        source_url: str,
        revision: str | None = None,
    ) -> ModelFiles:
        repo_id, endpoint, subpath = parse_hf_ref(source_url)
        rev = revision or "main"
        endpoint = (endpoint or settings.model_source_hf_endpoint).rstrip("/")
        timeout = settings.model_source_probe_timeout

        # —— meta + config 共用一个 client（复用连接池，省一次 TLS/握手）——
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # 先抓文件清单（成败的分水岭）
            try:
                # meta 也固定同一 rev：siblings/sha/pipeline_tag 与下方 raw/resolve 拉的
                # config、GGUF 头部必须同源同 commit，否则显式 revision 时清单来自
                # 默认分支、内容来自目标 rev，装配出跨版本的 ModelFiles
                resp = await client.get(f"{endpoint}/api/models/{repo_id}", params={"blobs": "true", "revision": rev})
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:  # 网络不通 / 仓库不存在
                log.warning("HF probe meta failed for %s: %s", repo_id, e)
                return ModelFiles(
                    success=False,
                    message=f"自动获取失败：无法访问 HuggingFace 仓库 {repo_id}（{type(e).__name__}）。"
                    "若国内网络不通，可配置 LLMPOD_MODEL_SOURCE_HF_ENDPOINT=https://hf-mirror.com",
                )

            siblings = data.get("siblings") or []
            # 全量清单（收窄前）：config 检查 + 视觉投影器探测都用它——用户贴具体变体文件时
            # siblings 会被收窄到那一个文件，而 mmproj 往往与变体不同名（如仓库根目录）
            all_names = inference._sibling_names(siblings)
            names_lower = [n.lower() for n in all_names]  # config 检查用（root）
            pipeline_tag = data.get("pipeline_tag")
            sha = data.get("sha")

            # —— 按 subpath 收窄（GGUF 多变体 repo：用户贴变体路径时只算该路径下文件）——
            if subpath:
                sub_low = subpath.lower()
                siblings = [
                    s
                    for s in siblings
                    if (inference._sibling_name(s)).lower() == sub_low
                    or (inference._sibling_name(s)).lower().startswith(sub_low + "/")
                ]

            # —— 抓 config（transformers 优先；否则 diffusers model_index）——
            config: dict = {}
            model_index: dict | None = None
            config_error: str | None = None
            if "config.json" in names_lower:
                try:
                    r = await client.get(f"{endpoint}/{repo_id}/raw/{rev}/config.json")
                    r.raise_for_status()
                    config = r.json()
                except Exception as e:
                    log.warning("HF probe config.json failed for %s: %s", repo_id, e)
                    config_error = f"config.json: {type(e).__name__}"
            elif "model_index.json" in names_lower:
                try:
                    r = await client.get(f"{endpoint}/{repo_id}/raw/{rev}/model_index.json")
                    r.raise_for_status()
                    model_index = r.json()
                except Exception as e:
                    # gated 仓库（如 FLUX.1-dev）无 token 取不到 → 非致命，靠文件结构继续
                    log.warning("HF probe model_index.json failed for %s: %s", repo_id, e)
                    config_error = f"model_index.json: {type(e).__name__}"

            # —— 远程 GGUF 头部 metadata 探 MoE 与架构形状：单变体 GGUF 无 config → Range 拉首片头解析 ——
            gguf_header = None
            scoped_names = [inference._sibling_name(s) for s in siblings]
            if inference.infer_format([n.lower() for n in scoped_names]) == "gguf":
                rel = inference.first_gguf_shard(scoped_names)
                if rel:
                    gguf_header = await remote_gguf.probe_gguf_metadata_remote(
                        client, f"{endpoint}/{repo_id}/resolve/{rev}/{rel}"
                    )

        return build_model_files(
            siblings,
            pipeline_tag,
            config=config,
            model_index=model_index,
            # 传原始 revision（可能 None），让 build_model_files 的 ``revision or sha`` 在未显式
            # 指定版本时固定到探测到的 commit sha——否则 revision 恒为 "main"，仓库前进了目录
            # 名不变、node 服务旧权重（内容漂移）。
            revision=revision,
            sha=sha,
            config_error=config_error,
            gguf_header=gguf_header,
            source_subpath=subpath,
            repo_names=all_names,
        )
