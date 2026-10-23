"""对象存储（OSS / S3 / COS）直链模型源探测器（manager 侧 httpx，**不下载**）。

OSS URL 通常是**单文件**直链（多为 gguf）：HEAD 取大小，URL 扩展名取格式，文件名取量化。
单文件无 config.json → 架构/类别无法自动判定（``source_kind=file``），返回有限信息 + 提示。
OSS 「目录/前缀」形式需要 ListObjects + 凭据，当前不支持，仅支持单文件直链。
"""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

import httpx

from llmpod.apps.manager.model_source import inference
from llmpod.apps.manager.model_source import remote_gguf
from llmpod.apps.manager.model_source.base import ModelFiles, ModelInspector
from llmpod.config.settings import settings

log = logging.getLogger("llmpod.apps.manager.model_source.oss")


def _filename_from_url(url: str) -> str:
    """从 URL 取末段文件名（去 query、decode）。"""
    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


class OSSInspector(ModelInspector):
    """OSS / S3 / COS 单文件直链探测器。"""

    async def inspect(
        self,
        *,
        source_url: str,
        revision: str | None = None,
    ) -> ModelFiles:
        timeout = settings.model_source_probe_timeout
        fname = _filename_from_url(source_url)

        gguf_meta = None
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.head(source_url)
                # 部分对象存储不支持 HEAD → 退回 streamed GET 只取响应头（不读 body）
                # 用 stream 避免多 GB 文件被非流式 get 全量读入内存
                if resp.status_code >= 400:
                    req = client.build_request("GET", source_url, headers={"Range": "bytes=0-0"})
                    resp = await client.send(req, stream=True)
                resp.raise_for_status()
                # 预取所有需要的响应头后立即关流，丢弃 body
                content_length = resp.headers.get("content-length")
                content_range = resp.headers.get("content-range")
                await resp.aclose()

                # —— 远程 GGUF 头部 metadata：单文件 GGUF 无 config，MoE 与架构形状都写在头里 ——
                # 取完整 metadata（不是只取 MoE 布尔）：同一个 Range 读里已经解析出来了，
                # 丢掉的话 tp 整除校验与上下文钳制在这条源上就永远拿不到输入
                if fname.lower().endswith(".gguf"):
                    gguf_meta = await remote_gguf.probe_gguf_metadata_remote(client, source_url)
        except Exception as e:
            log.warning("OSS probe failed for %s: %s", source_url, e)
            return ModelFiles(
                success=False,
                message=f"自动获取失败：无法访问对象存储地址（{type(e).__name__}）",
            )

        size_gb: float | None = None
        # 仅当 content-range 存在且含 /total 时才信其 total；否则 Range 请求的 content-length
        # 只是分片大小（如 bytes=0-0 → 1），不能当文件总大小，置 None 优于误报 0.0。
        if content_range and "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[-1])
                size_gb = round(total / (1024**3), 2)
            except (TypeError, ValueError):
                pass
        elif content_length and not content_range:
            # HEAD 成功（无 Range）→ content-length 即文件总大小
            try:
                size_gb = round(int(content_length) / (1024**3), 2)
            except (TypeError, ValueError):
                pass

        names = [fname] if fname else []
        fmt = inference.infer_format(names)
        # 架构形状：单文件无 config，只能来自 GGUF 头（与 HF/local 两条源同源装配）
        shape = inference.arch_shape({}, gguf_meta)
        return ModelFiles(
            success=inference.classify_success(fmt, size_gb),
            structure="single_file",
            format=fmt,
            layout=inference.detect_layout(names),
            has_config=False,
            size_gb=size_gb,
            is_moe=gguf_meta.is_moe if gguf_meta is not None else False,
            quantization=inference.infer_quant({}, names, gguf_meta),
            num_layers=shape.num_layers,
            num_attention_heads=shape.num_attention_heads,
            num_kv_heads=shape.num_kv_heads,
            head_dim=shape.head_dim,
            max_context=shape.max_context,
            revision=revision,
            weight_files=names or None,
            suggested_type=None,  # 非 HF 源：type 由用户选
            message="对象存储单文件：已获取大小/格式，身份信息有限，type 需人工选择",
        )
