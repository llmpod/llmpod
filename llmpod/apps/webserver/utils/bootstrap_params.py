"""节点引导脚本的启动参数模型（bootstrap-token 签发端与 bootstrap 渲染端共用）。

参数以 query 形式拼在引导 URL 上（只有非默认值会出现），不签名进引导令牌：
内部系统管理员使用，防篡改不是威胁模型（持令牌者拿到的脚本本就内嵌集群共享
Node Token）。真正的硬约束是这些值会被**文本嵌入** ``curl | bash`` 脚本——
每个字段按白名单字符集校验（无空白/引号/``$``/``;&|`` 等），既挡 shell 注入，
也保证 URL 里永不出现需百分号编码的字符（复制粘贴不被空格或 ``&`` 截断）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, Field

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.core.netaddr import clean_advertise_ip
from llmpod.services.version_service import official_image_ref

# docker 容器/具名卷名规则：字母数字开头，后续字母数字._-，64 字符内
_NAME_RE = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}"
# 数据卷/挂载 src：具名卷 或 宿主绝对路径（白名单形态；.. 段与裸 / 由
# _check_host_path 显式拒绝）
_VOLUME_RE = rf"(?:{_NAME_RE}|/[A-Za-z0-9._/-]{{0,255}})"
# 容器内绝对路径（段内字符白名单；".." 段另行显式拒绝）
_ABS_PATH_RE = r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?"
# 挂载模式：ro / rw / z / ro,z …（docker 传播/label 选项，逗号串联）
_MOUNT_MODE_RE = r"[A-Za-z][A-Za-z,]{0,15}"
# 挂载项 src:dst[:mode]（src=卷名或宿主绝对路径，dst=容器内绝对路径）
_MOUNT_RE = rf"(?P<src>{_VOLUME_RE}):(?P<dst>{_ABS_PATH_RE})(?::(?P<mode>{_MOUNT_MODE_RE}))?"
# 镜像 ref：registry:5000/repo:tag@sha256:… 全形态（docker tag 语法不含 +，收窄免歧义）
_IMAGE_RE = r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,255}"
# 兜底：每个待渲染进脚本的值必须 fullmatch（禁空白/引号/元字符/花括号）
_SAFE_RE = r"[A-Za-z0-9._:/@,-]{0,300}"

# 环境默认值兜底（容器名/卷名）
DEFAULT_CONTAINER_NAME = "llmpod-node"
DEFAULT_DATA_VOLUME = "llmpod-node-data"
# 默认镜像跟随 manager 自身版本（official_image_ref = 官方镜像:<__version__>，
# 发版纪律保证每个版本号都有对应 tag）：beta/test 管理器引导出的节点与自身同代，
# 不再被写死的 :latest 拉回 stable 旧版。显式传 image= 或目标机 env
# LLMPOD_NODE_IMAGE 仍可覆盖（本地构建 tag / 私有 registry）。
DEFAULT_IMAGE = official_image_ref()
# extra_mounts 条数上限（防 URL 无限加长）
MAX_EXTRA_MOUNTS = 16


def _reject(field: str, value: str, rule: str) -> None:
    raise LLMPodValidationError(f"引导参数 {field} 不合法（{rule}）: {value!r}")


def _check(field: str, value: str, pattern: str, rule: str) -> None:
    if not re.fullmatch(pattern, value):
        _reject(field, value, rule)


def _reject_dotdot_segments(field: str, value: str) -> None:
    """路径段 ``..`` 一律拒绝（宿主 bind 路径防穿越，如 /data/../etc）。"""
    if ".." in value.split("/"):
        _reject(field, value, "路径段不允许为 ..")


def _check_abs_path(field: str, value: str) -> None:
    """绝对路径校验：白名单字符 + 拒 ``..`` 段（防穿越/注入）。"""
    _check(field, value, _ABS_PATH_RE, "须为绝对路径且仅含 [A-Za-z0-9._-]/")
    _reject_dotdot_segments(field, value)


def _check_host_path(field: str, value: str) -> None:
    """宿主侧路径（数据卷 bind 形态 / 挂载 src）：具名卷 或 绝对路径。

    绝对路径额外拒 ``..`` 段与裸 ``/``——该值会成为 ``-v src:dst`` 的宿主侧
    源路径，配合容器内的 docker.sock 不应把宿主根目录整棵挂进节点容器。
    """
    if value.startswith("/"):
        _check_abs_path(field, value)
        if value == "/":
            _reject(field, value, "不允许挂载宿主根目录")
    else:
        _check(field, value, _NAME_RE, "具名卷规则 [a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")


class BootstrapNodeParams(BaseModel):
    """节点引导的启动参数（query 传入，渲染进 addnode 模板）。

    容器内模型路径不是用户参数：canonical 路径是集群级不变量（manager 算好下发、
    多机 TP 要求各机同路径），恒取 manager 的 ``model_store_dir``，由 ``resolved()``
    填入。``model_host_dir`` 是唯一与模型存储相关的用户参数：宿主大盘在别处时填
    实际路径（引导脚本校验存在后 bind 到 canonical 路径）；留空 = 宿主直接使用
    canonical 路径（脚本 mkdir -p 后同路径挂载）。
    """

    node_ip: str = ""  # 本机对其他节点的互联 IP；缺省则目标机引导脚本交互强制选定（无终端时必须传）
    container_name: str = DEFAULT_CONTAINER_NAME
    data_volume: str = DEFAULT_DATA_VOLUME
    model_host_dir: str = ""
    extra_mounts: list[str] = Field(default_factory=list)
    image: str = DEFAULT_IMAGE
    # 非用户输入：resolved(default_model_dir) 填 manager 的 model_store_dir
    model_dir: str = ""

    @classmethod
    def from_raw(
        cls,
        *,
        node_ip: str | None = None,
        container_name: str | None = None,
        data_volume: str | None = None,
        model_host_dir: str | None = None,
        extra_mounts: Sequence[str] | None = None,
        image: str | None = None,
    ) -> Self:
        """从路由层原始入参构建并逐项校验，非法值 raise LLMPodValidationError（422）。

        None / 缺省 → 字段默认值；空串 node_ip 合法（目标机交互菜单选定——无终端
        环境不合法，脚本会拒绝继续，自动化装机必须在引导命令带 node_ip）。
        （node_ip 对外命名；落进容器是 env LLMPOD_NODE_ADVERTISE_IP，内部命名沿用既有配置）
        """
        p = cls(
            node_ip=(node_ip or "").strip(),
            container_name=container_name or DEFAULT_CONTAINER_NAME,
            data_volume=data_volume or DEFAULT_DATA_VOLUME,
            model_host_dir=model_host_dir or "",
            extra_mounts=list(extra_mounts) if extra_mounts else [],
            image=image or DEFAULT_IMAGE,
        )
        p.validate_all()
        return p

    def validate_all(self) -> None:
        """逐字段校验（mint 与渲染两端共用同一套规则）。"""
        if self.node_ip:
            # 可用性判定走 core.netaddr.clean_advertise_ip（与 manager $hello 选定流程
            # 共用同一权威，引导脚本 valid_ipv4 是跨语言镜像）：此处放行的值 manager
            # 才会采纳，坏值在签发端就 422，不等到装机/落库才暴露。
            if not clean_advertise_ip(self.node_ip):
                _reject(
                    "node_ip",
                    self.node_ip,
                    "须为可用 IPv4 单播地址（未指定段/回环/链路本地/组播/保留段/前导零不可用）",
                )
        _check("container_name", self.container_name, _NAME_RE, "docker 容器名规则 [a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
        _check_host_path("data_volume", self.data_volume)
        if self.model_host_dir:
            _check_abs_path("model_host_dir", self.model_host_dir)
        if len(self.extra_mounts) > MAX_EXTRA_MOUNTS:
            _reject("extra_mounts", f"{len(self.extra_mounts)} 条", f"最多 {MAX_EXTRA_MOUNTS} 条")
        seen: set[str] = set()
        for m in self.extra_mounts:
            if not m or not re.fullmatch(_MOUNT_RE, m):
                _reject(
                    "extra_mounts", m, "须为 src:dst[:mode]（src=卷名/宿主绝对路径，dst=容器内绝对路径，mode 如 ro/z）"
                )
            if m in seen:
                _reject("extra_mounts", m, "重复挂载项")
            seen.add(m)
            # src 半段是宿主侧路径，与 data_volume 同规则（拒 .. 段与裸 /）
            src = m.split(":", 1)[0]
            _check_host_path("extra_mounts", src)
        _check("image", self.image, _IMAGE_RE, "镜像 ref（registry:5000/repo:tag@digest）")
        # 渲染值最终闸：模板把值文本嵌入 bash，白名单外的任何字符都不放行
        # （逐项检查；extra_mounts 的空格 join 分隔符不算值内容）
        for field, value in [
            *self.render_items(exclude_mounts_join=True).items(),
            *[("extra_mounts", m) for m in self.extra_mounts],
        ]:
            if not re.fullmatch(_SAFE_RE, value):
                _reject(field, value, "含非法字符")

    def resolved(self, default_model_dir: str) -> Self:
        """填 canonical 容器内模型路径，返回新实例（不改动自身）。

        model_dir 恒 = manager 的 model_store_dir（集群级不变量：canonical 路径全
        节点一致，多机 TP 同路径）；model_host_dir 留空时同 canonical（宿主同路径
        挂载，脚本自动 mkdir）。默认值来自 manager 自身配置（不经 from_raw 入口
        校验），渲染前同样过白名单：配置里混入空白/元字符时在引导端点即失败，
        而不是产出坏脚本。
        """
        resolved = self.model_copy(
            update={
                "model_dir": default_model_dir,
                "model_host_dir": self.model_host_dir or default_model_dir,
            }
        )
        resolved.validate_all()
        return resolved

    def query_items(self, default_model_dir: str) -> list[tuple[str, str]]:
        """仅输出非默认值 → URL query 对（extra_mounts 每项一对，顺序稳定）。

        不传参数时返回空表 → 签发出的 command 与旧格式逐字节一致。
        model_host_dir 仅在与 canonical 路径不同时出现（= 大盘在别处）。
        """
        items: list[tuple[str, str]] = []
        if self.node_ip:
            items.append(("node_ip", self.node_ip))
        if self.container_name != DEFAULT_CONTAINER_NAME:
            items.append(("container_name", self.container_name))
        if self.data_volume != DEFAULT_DATA_VOLUME:
            items.append(("data_volume", self.data_volume))
        if self.model_host_dir and self.model_host_dir != default_model_dir:
            items.append(("model_host_dir", self.model_host_dir))
        items.extend(("extra_mounts", m) for m in self.extra_mounts)
        if self.image != DEFAULT_IMAGE:
            items.append(("image", self.image))
        return items

    def render_items(self, exclude_mounts_join: bool = False) -> dict[str, str]:
        """占位符名 → 渲染值（extra_mounts 空格 join；调用方须先 ``resolved``）。

        ``exclude_mounts_join=True`` 供校验用：extra_mounts 置空串，逐项由调用方检查。
        """
        return {
            "node_ip": self.node_ip,
            "container_name": self.container_name,
            "data_volume": self.data_volume,
            "model_dir": self.model_dir,
            "model_host_dir": self.model_host_dir,
            "extra_mounts": "" if exclude_mounts_join else " ".join(self.extra_mounts),
            "image": self.image,
        }


__all__ = ["DEFAULT_CONTAINER_NAME", "DEFAULT_DATA_VOLUME", "DEFAULT_IMAGE", "BootstrapNodeParams"]
