"""部署执行器工厂。

按 ``settings.deployer_type``（env ``LLMPOD_DEPLOYER_TYPE``）选实现：
- ``docker``（默认）→ ``DockerDeployer``
- ``k8s``（将来）→ ``K8sDeployer``（当前显式 ``ConfigError`` 拒绝，接入时替换）
"""

from __future__ import annotations

from llmpod.apps.node.deployers.base import Deployer
from llmpod.apps.node.deployers.docker import DockerDeployer
from llmpod.core.exceptions import ConfigError

__all__ = ["Deployer", "DockerDeployer", "make_deployer"]


def make_deployer(settings) -> Deployer:
    """按 ``settings.deployer_type`` 构造 ``Deployer``。

    严格匹配：``docker`` → ``DockerDeployer``；``k8s`` → ``ConfigError``（未实现）；
    其它/拼写错误 → ``ValueError``（不静默降级，便于排障）。
    """
    dtype = (getattr(settings, "deployer_type", "") or "").strip().lower()
    if dtype in ("", "docker"):
        return DockerDeployer()
    if dtype == "k8s":
        raise ConfigError("K8sDeployer not implemented（本次仅留抽象；K8s 接入）")
    raise ConfigError(f"unknown LLMPOD_DEPLOYER_TYPE={dtype!r}（合法值：docker | k8s）")
