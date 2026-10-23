"""DeviceConfig：arch-keyed 设备配置（runtime 无关），适配器产出、编排器消费转 DeploySpec。

gpus 为宿主设备索引列表；None=不申请 GPU 设备（cann/cpu 走 runtime+extra_devices），
[] 空表=全部 GPU（nvidia device_requests count=-1），[0,1]=指定卡。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeviceConfig(BaseModel):
    """arch-keyed 设备配置（runtime 无关）。"""

    model_config = ConfigDict(extra="forbid")

    runtime: str | None = None
    gpus: list[int] | None = []
    driver_mounts: list[str] = []
    privileged: bool = False
    cap_add: list[str] = []
    extra_devices: list[str] = []  # ["host:cont[:perm]"]，如 Ascend /dev/davinci*
