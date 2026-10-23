"""WebSocket 入场：共享节点令牌，首次登记额外要求短期引导令牌。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from llmpod.apps.manager import bootstrap
from llmpod.apps.webserver.utils.bootstrap_token import mint_bootstrap_token
from llmpod.models.node import Node
from tests.integration._fakes import FakeSession


@pytest.mark.asyncio
async def test_upgrade_auth_rejects_unauthorized_and_unknown_node(monkeypatch):
    db = FakeSession()
    db.preload(Node(id="known", cluster_id=1, hostname="h", ip="10.0.0.1"))

    @asynccontextmanager
    async def db_context():
        yield db

    async def not_deleted(_node_id):
        return False

    monkeypatch.setattr(bootstrap, "get_db_context", db_context)
    monkeypatch.setattr(bootstrap, "node_tombstone_hit", not_deleted)
    monkeypatch.setattr(bootstrap, "bootstrap_signing_key", lambda _settings: "signing-key")
    ticket, _ = mint_bootstrap_token("signing-key", 1, 3600)
    lifespan = SimpleNamespace(node_token="shared-secret", settings=object())

    def headers(node_id="", secret="shared-secret", ticket_value=""):
        result = {"authorization": f"Bearer {secret}", "x-llmpod-cluster-id": "1"}
        if node_id:
            result["x-llmpod-node-id"] = node_id
        if ticket_value:
            result["x-llmpod-bootstrap-token"] = ticket_value
        return result

    assert await bootstrap.authenticate_upgrade(headers("known"), lifespan)
    assert await bootstrap.authenticate_upgrade(headers("known", "wrong"), lifespan) is None
    assert await bootstrap.authenticate_upgrade(headers("unknown", ticket_value=ticket), lifespan) is None
    assert await bootstrap.authenticate_upgrade(headers(), lifespan) is None
    admitted = await bootstrap.authenticate_upgrade(headers(ticket_value=ticket), lifespan)
    assert admitted and admitted.node_id == "" and admitted.bootstrap_token == ticket
    assert (
        await bootstrap.authenticate_upgrade(headers(ticket_value=ticket) | {"x-llmpod-cluster-id": "2"}, lifespan)
        is None
    )
    assert await bootstrap.authenticate_upgrade({}, lifespan) is None


@pytest.mark.asyncio
async def test_websocket_rejects_missing_credentials_before_accept(monkeypatch):
    from llmpod.apps.manager import app as manager_app

    class Hub:
        async def accept(self, _websocket):
            raise AssertionError("unauthorized connection was accepted")

    class WebSocket:
        headers = {}

        def __init__(self):
            self.closed_code = None

        async def close(self, *, code, reason=None):
            self.closed_code = code

    monkeypatch.setattr(manager_app, "node_hub", Hub())
    websocket = WebSocket()
    await manager_app.create_manager_router().routes[0].endpoint(websocket)
    assert websocket.closed_code == 1008
