"""受控 HTTP 推理上游；只验证协议/网关链路，不执行真实模型推理。

宿主启动：python tests/e2e/_gateway_upstream.py --host 127.0.0.1 --port 18091
QEMU SLIRP guest 内 api_base 使用 http://10.0.2.2:18091/v1。
现有 test_affinity_external.py 可复用此服务，模型名分别使用下列 CHAT_MODEL / ANTHROPIC_MODEL。
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_KEY = "e2e-upstream"
CHAT_MODEL = "gateway-chat-fixture"
ANTHROPIC_MODEL = "gateway-anthropic-fixture"
EMBEDDING_MODEL = "gateway-embedding-fixture"
RERANK_MODEL = "gateway-rerank-fixture"
REPLY = "controlled upstream reply"
PROMPT_TOKENS = 12
COMPLETION_TOKENS = 4


class GatewayUpstream(BaseHTTPRequestHandler):
    api_key = DEFAULT_KEY

    def _json(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, frames):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for event, data in frames:
            prefix = f"event: {event}\n" if event else ""
            payload = data if isinstance(data, str) else json.dumps(data)
            self.wfile.write(f"{prefix}data: {payload}\n\n".encode())
            self.wfile.flush()

    def do_GET(self):
        self._json(200 if self.path == "/health" else 404, {"service": "controlled-gateway-upstream"})

    def do_POST(self):
        expected_model = {
            "/v1/chat/completions": CHAT_MODEL,
            "/v1/messages": ANTHROPIC_MODEL,
            "/v1/messages/count_tokens": ANTHROPIC_MODEL,
            "/v1/embeddings": EMBEDDING_MODEL,
            "/v1/rerank": RERANK_MODEL,
        }.get(self.path)
        if expected_model is None:
            self._json(404, {"error": {"message": "unknown fixture path"}})
            return
        anthropic = self.path.startswith("/v1/messages")
        authenticated = (
            self.headers.get("x-api-key") == self.api_key
            if anthropic
            else self.headers.get("Authorization") == f"Bearer {self.api_key}"
        )
        if not authenticated:
            self._json(401, {"error": {"message": "invalid upstream key"}})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except ValueError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return
        if not isinstance(body, dict) or body.get("model") != expected_model:
            self._json(400, {"error": {"message": "gateway did not forward the configured served_model"}})
            return
        if anthropic and not self.headers.get("anthropic-version"):
            self._json(400, {"error": {"message": "missing anthropic-version"}})
            return

        request_id = uuid.uuid4().hex
        model = body["model"]
        usage = {
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
        }
        if self.path.endswith("/count_tokens"):
            self._json(200, {"input_tokens": PROMPT_TOKENS})
        elif self.path.endswith("/chat/completions"):
            base = {"id": f"chatcmpl-{request_id}", "model": model, "created": 1}
            if body.get("stream"):
                base["object"] = "chat.completion.chunk"
                self._sse(
                    [
                        (
                            None,
                            {
                                **base,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"role": "assistant", "content": REPLY},
                                        "finish_reason": None,
                                    }
                                ],
                            },
                        ),
                        (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                        (None, {**base, "choices": [], "usage": usage}),
                        (None, "[DONE]"),
                    ]
                )
            else:
                self._json(
                    200,
                    {
                        **base,
                        "object": "chat.completion",
                        "usage": usage,
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}
                        ],
                    },
                )
        elif self.path.endswith("/messages"):
            message = {
                "id": f"msg-{request_id}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": REPLY}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": COMPLETION_TOKENS},
            }
            if body.get("stream"):
                events = [
                    {
                        "type": "message_start",
                        "message": {
                            **message,
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": 0},
                        },
                    },
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": REPLY}},
                    {"type": "content_block_stop", "index": 0},
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": COMPLETION_TOKENS},
                    },
                    {"type": "message_stop"},
                ]
                self._sse((event["type"], event) for event in events)
            else:
                self._json(200, message)
        elif self.path.endswith("/embeddings"):
            values = body.get("input", [])
            if isinstance(values, str):
                values = [values]
            self._json(
                200,
                {
                    "object": "list",
                    "model": model,
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [0.25, 0.5, 0.75]}
                        for i, _value in enumerate(values)
                    ],
                    "usage": {"prompt_tokens": PROMPT_TOKENS, "total_tokens": PROMPT_TOKENS},
                },
            )
        else:
            self._json(
                200,
                {
                    "id": request_id,
                    "model": model,
                    "results": [
                        {"index": i, "relevance_score": 1.0 / (i + 1)}
                        for i, _doc in enumerate(body.get("documents", []))
                    ],
                    "usage": {"prompt_tokens": PROMPT_TOKENS, "total_tokens": PROMPT_TOKENS},
                },
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18091)
    parser.add_argument("--key", default=os.environ.get("LLMPOD_E2E_GATEWAY_UPSTREAM_KEY", DEFAULT_KEY))
    args = parser.parse_args()
    GatewayUpstream.api_key = args.key
    with ThreadingHTTPServer((args.host, args.port), GatewayUpstream) as server:
        print(f"Controlled upstream listening on {args.host}:{args.port}; no real model inference", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
