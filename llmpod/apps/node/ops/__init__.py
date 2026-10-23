"""节点操作处理器：Shell（arch 无关通用执行器）。

  shell.py - Shell 命令执行

容器生命周期（run/stop/rm/list/inspect）已移至 ``apps/node/deployers/``（Deployer 抽象），
由 ``NodeServiceImpl`` 注入并薄派发，不留在本 arch 无关层。
"""

from llmpod.apps.node.ops.shell import execute_shell

__all__ = ["execute_shell"]
