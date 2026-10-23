"""JSON-RPC 2.0 协议帧构造函数单元测试。"""

from __future__ import annotations

from llmpod.tunnel.entity.protocol import (
    new_id,
    request,
    notification,
    success,
    stream_frame,
    stream_end,
    error,
    PARSE_ERR,
    SERVER_ERROR,
    NODE_OFFLINE,
)


class TestNewId:
    def test_returns_string(self):
        rid = new_id()
        assert isinstance(rid, str)

    def test_returns_12_chars(self):
        rid = new_id()
        assert len(rid) == 12

    def test_unique_ids(self):
        ids = {new_id() for _ in range(100)}
        assert len(ids) == 100


class TestRequestFrame:
    def test_structure(self):
        frame = request("Svc.method", {"a": 1}, "abc")
        assert frame == {
            "jsonrpc": "2.0",
            "method": "Svc.method",
            "params": {"a": 1},
            "id": "abc",
        }

    def test_has_id(self):
        frame = request("Svc.m", {}, "rid-1")
        assert "id" in frame
        assert frame["id"] == "rid-1"


class TestNotificationFrame:
    def test_no_id(self):
        frame = notification("$hello", {"node_id": "n1"})
        assert "id" not in frame
        assert frame["method"] == "$hello"
        assert frame["params"]["node_id"] == "n1"

    def test_structure(self):
        frame = notification("$heartbeat", {"info": {}})
        assert frame == {
            "jsonrpc": "2.0",
            "method": "$heartbeat",
            "params": {"info": {}},
        }


class TestSuccessFrame:
    def test_unary(self):
        frame = success("abc", {"result": 42})
        assert frame == {"jsonrpc": "2.0", "result": {"result": 42}, "id": "abc"}
        assert "stream" not in frame

    def test_with_stream(self):
        frame = success("abc", 42, stream=True)
        assert frame["stream"] is True

    def test_without_stream(self):
        frame = success("abc", 42, stream=False)
        assert "stream" not in frame


class TestStreamFrame:
    def test_structure(self):
        frame = stream_frame("abc", 42)
        assert frame == {"jsonrpc": "2.0", "result": 42, "id": "abc", "stream": True}


class TestStreamEnd:
    def test_structure(self):
        frame = stream_end("abc")
        assert frame == {"jsonrpc": "2.0", "result": None, "id": "abc"}
        assert "stream" not in frame


class TestErrorFrame:
    def test_basic(self):
        frame = error("abc", SERVER_ERROR, "something failed")
        assert frame["error"]["code"] == SERVER_ERROR
        assert frame["error"]["message"] == "something failed"
        assert "data" not in frame["error"]

    def test_with_data(self):
        frame = error("abc", PARSE_ERR, "bad json", data="traceback...")
        assert frame["error"]["data"] == "traceback..."

    def test_error_codes(self):
        assert PARSE_ERR == -32700
        assert SERVER_ERROR == -32000
        assert NODE_OFFLINE == -32001
