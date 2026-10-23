"""节点资源采集器：系统级 + GPU。

system.py - CPU/内存/swap/disk/load/OS（静态 + 运行时）
gpu.py    - GPU 显存/利用率/温度/功耗/时钟/PCIe 等
__init__  - 聚合出口 collect_node_info()，供 $hello/$heartbeat 与 RPC 查询共用
"""

from llmpod.apps.node.collectors.system import collect_system_info, collect_node_info
from llmpod.apps.node.collectors.gpu import collect_gpu_info

__all__ = ["collect_system_info", "collect_node_info", "collect_gpu_info"]
