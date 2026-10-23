"""Manager 端 Node→Manager RPC 服务实现包。

对称 ``apps/node/services``（Node 端 Manager→Node RPC 实现）：此处承载 Node 反向调
Manager 的契约实现。``ManagerTaskServiceImpl``（任务状态上报）。
"""

from llmpod.apps.manager.services.task_ingest import ManagerTaskServiceImpl

__all__ = ["ManagerTaskServiceImpl"]
