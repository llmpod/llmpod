"""节点端口试绑预检 RPC 单测（NodeServiceImpl.check_ports_free）。

真实 socket 驱动：保持监听的口 = 被占；绑定后释放的口 = 空闲。两个口都从内核
临时段取（bind 0.0.0.0:0 让内核分配），不依赖机器上的固定端口，测试确定性。
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

from llmpod.apps.node.services.node import NodeServiceImpl


def _svc() -> NodeServiceImpl:
    return NodeServiceImpl(SimpleNamespace(model_store_dir="/tmp", model_import_roots=""))


def _hold_port():
    """占住一个口（保持监听），返回 (socket, port)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    return s, port


def _free_port():
    """拿一个刚释放的口（bind 后立即 close，内核临时段、无监听者）。"""
    s, port = _hold_port()
    s.close()
    return port


class TestCheckPortsFree:
    async def test_held_port_reported_occupied(self):
        holder, occupied_port = _hold_port()
        free_port = _free_port()
        try:
            res = await _svc().check_ports_free([occupied_port, free_port])
            assert res.occupied == [occupied_port]
        finally:
            holder.close()

    async def test_all_free_returns_empty(self):
        res = await _svc().check_ports_free([_free_port(), _free_port()])
        assert res.occupied == []

    async def test_empty_input(self):
        res = await _svc().check_ports_free([])
        assert res.occupied == []
