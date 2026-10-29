"""本地路径模型源探测器（manager 侧，**直接读 manager 自己的盘**）。

local 路径指 manager 容器/进程可达的本地路径（典型 = ``POST /admin/models/upload``
落地的 staged 目录，或用户手动放进 ``model_store_dir`` 的路径）。manager 直接读
config.json + 列权重文件 → ``ModelProbe`` → ``build_local_model_files`` → ``ModelFiles``。

不再经隧道 RPC 让 node 探测：inspect 是注册期、用户态的预填助手，不应耦合 node 在线
与隧道。node 盘上的预置路径属部署期源（部署时 deployer 自己 cat config 选引擎参数）。

type 建议恒为 None（非 HF 源，用户选）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from llmpod.apps.manager.model_source import inference
from llmpod.apps.manager.model_source.base import ModelFiles, ModelInspector

log = logging.getLogger("llmpod.apps.manager.model_source.local")


class ModelProbe(BaseModel):
    """manager 本地探测中间类型（config 原文 + 权重清单 + 总字节）。

    由 ``_read_local_probe_sync`` 产、``build_local_model_files`` 消费，**不是 wire 契约**
    （node↔manager 隧道侧已无 inspect_model；仅 manager 内部用）。
    """

    model_config = ConfigDict(extra="ignore")

    exists: bool = False
    config: dict | None = None  # config.json 原文（若目录可读）
    config_name: str | None = None  # 读到的配置文件名：config.json | model_index.json（决定 layout/architecture 取法）
    weight_files: list[str] = []  # 权重文件相对路径（.safetensors/.gguf/.bin/.pt/.pth/.onnx）
    total_bytes: int = 0  # 权重文件总字节
    message: str | None = None  # 探测失败/异常说明


def build_local_model_files(probe: ModelProbe, path: str, revision: str | None = None) -> ModelFiles:
    """``ModelProbe`` → ``ModelFiles``（纯函数，离线可测）。

    type 建议恒为 None（local 无 pipeline_tag，用户选）；字段由 config/权重清单提取。
    """
    config = probe.config or {}
    is_model_index = probe.config_name == "model_index.json"
    # names 须含真实配置文件名：detect_layout 据此判 diffusers（model_index.json）
    # vs transformers（config.json）；不能统一写死 "config.json"。
    config_name = probe.config_name or ("config.json" if probe.config else None)
    names = ([config_name] if config_name else []) + list(probe.weight_files or [])
    # 单文件判定：仅一个权重文件且无 config（gguf/onnx 直链本地）
    if len(probe.weight_files) <= 1 and not probe.config:
        structure = "single_file"
    else:
        structure = "directory"
    fmt = inference.infer_format(names)
    # —— MoE 判定与架构形状：config 有则查 config；GGUF 无 config → 读文件头 metadata ——
    # GGUF 的 expert_count 与 block_count/head_count_kv/key_length 写在 .gguf 头的 KV 表
    # （不写在 config.json），故 GGUF 路径必须解析文件头；读不到（非 GGUF / 截断 / 第三方
    # 量化没写）降级为 None/False，不比现在差。
    # 有 config 也照样解析：GGUF 的 config.json 从不带量化信息，而 HF 路径是无条件把文件头
    # 传给 infer_quant 的——两条源口径必须一致，否则同一份产物在两处得到不同的量化值，
    # recipe_digest 随之分叉成两个下载目录。config 优先的地方（is_moe / arch_shape）不受影响。
    gguf_meta_obj = _infer_gguf_meta(probe, path, fmt)
    if probe.config:
        is_moe = inference.infer_moe(config)
    else:
        is_moe = bool(gguf_meta_obj and gguf_meta_obj.is_moe)
    size_gb = round(probe.total_bytes / (1024**3), 2) if probe.total_bytes else None
    # 架构形状：manager 盘上有 config 就以它为准（更权威的描述），否则取 GGUF 头
    shape = inference.arch_shape(config, gguf_meta_obj)
    # 盘上布局（node 分发判据）：与 inference.detect_source_kind 同口径——local 也在 manager
    # 探测期推导 source_kind，create 路由据此落 Model.source_kind。
    source_kind = inference.detect_source_kind(names)
    return ModelFiles(
        success=inference.classify_success(fmt, size_gb),
        structure=structure,
        source_kind=source_kind,
        format=fmt,
        layout=inference.detect_layout(names),
        has_config=bool(probe.config),
        weight_files=probe.weight_files or None,
        # diffusers 的 _class_name 在 model_index.json 里，须传进去才取得到
        architecture=inference.infer_architecture(config, probe.config if is_model_index else None),
        is_moe=is_moe,
        is_vision=inference.has_mmproj(probe.weight_files or []),
        mmproj_file=inference.pick_mmproj(probe.weight_files or []),
        quantization=inference.infer_quant(config, probe.weight_files or [], gguf_meta_obj),
        size_gb=size_gb,
        num_layers=shape.num_layers,
        kv_layers=shape.kv_layers,
        num_attention_heads=shape.num_attention_heads,
        num_kv_heads=shape.num_kv_heads,
        head_dim=shape.head_dim,
        max_context=shape.max_context,
        needs_trust_remote_code=inference.needs_trust_remote_code(config or {}),
        revision=revision,
        suggested_type=None,
        message=None,
    )


def _infer_gguf_meta(probe: ModelProbe, path: str, fmt: str | None):
    """从 GGUF 文件头取 MoE、架构形状与量化类型；非 GGUF / 读不到 → None。

    ``probe.weight_files`` 是相对路径，单文件源下相对 ``path`` 的父目录、目录源下相对
    ``path`` 本身（与 ``_read_local_probe_sync`` 的落盘口径一致），所以先定出 ``base`` 再拼。

    「哪个是模型文件」交给 ``first_gguf_shard``：它已排掉 mmproj（视觉投影器不是模型本体）
    并挑出多分片的第 0 片（metadata 只在首片头）。单文件源同样走它——直接读 ``path``
    会把投影器当模型，层数/视觉标记/量化全错。
    """
    if fmt != "gguf" or not probe.weight_files:
        return None
    from llmpod.apps.manager.model_source import gguf_meta
    from llmpod.apps.manager.model_source.inference import first_gguf_shard

    root = Path(path)
    base = root.parent if root.is_file() else root
    rel = first_gguf_shard(probe.weight_files)
    if not rel:
        return None
    return gguf_meta.parse_gguf_metadata(base / rel)


def _read_local_probe_sync(path: Path) -> ModelProbe:
    """同步读 manager 本地路径 → ``ModelProbe``（config 原文 + 权重清单 + 总字节）。

    - 目录：读 ``config.json``（transformers）/ ``model_index.json``（diffusers）；
      ``os.walk`` 找权重扩展名文件，累加大小 + 相对路径。
    - 单文件（gguf/onnx 直链本地）：config=None，weight_files=[name]，total_bytes=size。
    """
    if not path.exists():
        # 文案与「越界」共用（不区分不存在/不可达，防探测语义泄露 store 结构）；
        # 不带绝对路径——调用方回显用户输入的 source_url 即可定位
        return ModelProbe(exists=False, message="本地路径不存在或不可达")

    # 单文件直链
    if path.is_file():
        try:
            total = path.stat().st_size
        except OSError as e:
            return ModelProbe(exists=False, message=f"stat 失败：{e}")
        return ModelProbe(exists=True, config=None, weight_files=[path.name], total_bytes=total)

    # 目录
    config: dict | None = None
    config_name: str | None = None
    for cfg_name in ("config.json", "model_index.json"):
        cfg_path = path / cfg_name
        if cfg_path.is_symlink():
            log.warning("local inspect 拒绝符号链接配置文件：%s", cfg_path)
            continue
        if cfg_path.is_file():
            try:
                fd = os.open(cfg_path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
                if not isinstance(loaded, dict):
                    # 顶层非 JSON object（数组/字符串/数字都是合法 JSON）：按「无可用
                    # config」降级继续，不能喂给 ModelProbe.config（dict | None）
                    log.warning("%s 顶层不是 JSON object，忽略：%s", cfg_name, path)
                    continue
                config = loaded
                config_name = cfg_name
                break
            except (OSError, ValueError) as e:
                # ValueError 同时覆盖 JSONDecodeError 与 UnicodeDecodeError（GBK/UTF-16
                # 等非 UTF-8 编码的 config.json 很常见）——探测失败一律软失败，不 500
                log.warning("read %s failed for %s: %s", cfg_name, path, e)

    weight_files: list[str] = []
    total = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            low = fname.lower()
            # 权重扩展名之外，投影器候选也要收集（识别口径归 inference：
            # 名字含 mmproj 即投影器，*.mmproj 等非权重扩展名命名同样算）
            if not low.endswith(inference.WEIGHT_EXTS) and not inference.is_mmproj(low):
                continue
            fp = Path(root) / fname
            if fp.is_symlink():
                continue
            try:
                metadata = fp.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                size = metadata.st_size
            except OSError:
                continue
            total += size
            weight_files.append(str(fp.relative_to(path)))

    return ModelProbe(
        exists=True,
        config=config,
        config_name=config_name,
        weight_files=weight_files,
        total_bytes=total,
        message=None,
    )


class LocalInspector(ModelInspector):
    """本地路径探测器：manager 直接读自己盘上的路径。

    出于防「借探测接口枚举 manager 任意目录」，``source_url`` 必须落在
    ``settings.model_store_dir`` 之内（resolve + 前缀校验，拒绝符号链接逃逸）；
    越界路径一律按「不可达」软失败回显，不区分「不存在/越界」以免探测语义
    泄露根目录结构。上传接口（/admin/models/upload）与手动放入的路径都在
    该根之下，正常流程不受影响。
    """

    async def inspect(
        self,
        *,
        source_url: str,
        revision: str | None = None,
    ) -> ModelFiles:
        from llmpod.config.settings import settings
        from llmpod.utils.safe_path import safe_resolve

        store_root = Path(settings.model_store_dir).expanduser().resolve()
        resolved = safe_resolve(store_root, source_url)
        if resolved is None:
            log.warning("local inspect 越界路径已拒绝：%s", source_url)
            # 与「不存在」共用同一文案：可区分的回显会向调用方泄露 store 边界判定
            # 结果，正是类 docstring 承诺要抑制的探测信号
            return ModelFiles(success=False, message=f"本地路径不存在或不可达：{source_url}")
        path = resolved

        probe = await asyncio.to_thread(_read_local_probe_sync, path)
        if not probe.exists:
            return ModelFiles(success=False, message=probe.message or f"本地路径不存在或不可达：{source_url}")
        # 装配也放线程里：GGUF 源要读最多 32 MiB 文件头并整表遍历 KV，同步跑会占住
        # 管理端的事件循环（它同时驱动 node 反向 WS 心跳与 reconciler）。
        return await asyncio.to_thread(build_local_model_files, probe, str(path), revision)
