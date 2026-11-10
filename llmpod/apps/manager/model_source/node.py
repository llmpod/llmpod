"""节点目录模型源探测器（manager 侧，经隧道读节点盘）。

与 ``local`` 探测器的区别只有数据来源：目录事实由节点 RPC 返回，识别装配复用
``build_local_model_files``。节点只提供 config 与文件清单，不做模型语义判断；manager
读不到节点盘上的权重文件，GGUF 头元数据按缺失降级（格式/大小仍可识别）。节点离线或
目录无效时返回软失败（不抛 500）。
"""

from __future__ import annotations

from llmpod.apps.manager.model_source.base import ModelFiles, ModelInspector
from llmpod.apps.manager.model_source.inference import WEIGHT_EXTS
from llmpod.core.exceptions import LLMPodError


class NodeInspector(ModelInspector):
    """经隧道探测某台节点上的模型目录。"""

    def __init__(self, node_id: str):
        self._node_id = node_id

    async def inspect(
        self,
        *,
        source_url: str,
        revision: str | None = None,
    ) -> ModelFiles:
        from llmpod.apps.manager import dispatch
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        try:
            probe = await dispatch.probe_model_dir(self._node_id, source_url)
        except LLMPodError as e:
            return ModelFiles(success=False, message=f"节点不可达或探测失败：{e}")

        if not probe.exists or not probe.is_dir:
            return ModelFiles(success=False, message=probe.message or "节点目录不可用")

        weights = [entry for entry in probe.files if entry.path.lower().endswith(WEIGHT_EXTS)]
        if not weights:
            return ModelFiles(success=False, message=probe.message or "目录内没有权重文件")

        intermediate = ModelProbe(
            exists=True,
            config=probe.config,
            config_name=probe.config_name,
            weight_files=[entry.path for entry in weights],
            total_bytes=sum(entry.size for entry in weights),
            message=probe.message,
        )
        # 权重文件在节点盘上，manager 读不到 GGUF 头；显式传 None 跳过本地读头
        files = build_local_model_files(intermediate, source_url, revision, gguf_meta=None)
        if probe.message:
            # 节点侧降级说明（如 config 解析失败）带到响应里，前端可直接展示
            files = files.model_copy(update={"message": probe.message})
        return files
