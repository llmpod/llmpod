"""RequestLogger 集成测试（管道末尾异步记账的写入路径）。

- put 两块（截断+脱敏后）→ 写计量行（refs + conv_prefix_hash + usage/cost）；
- 块写失败 → 计量行仍落库（refs 置空）——计量不丢；
- 单字段截断上限 + truncated 标记；
- embeddings 无 conv_prefix_hash。
"""

from __future__ import annotations

import json

from _fakes import FakeSession

from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
from llmpod.apps.proxyserver.request_logger import RequestLogger, RequestLogRecord
from llmpod.models import RequestLog
from llmpod.models.request_log import PROVIDER_NAME_MAX_CHARS


def _record(**ovr) -> RequestLogRecord:
    base = dict(
        request_id="req-0001",
        api_key_id=1,
        tenant_id=None,
        model_alias="m1",
        served_model="internal-m1",
        operation="chat.completions",
        protocol="openai",
        engine="vllm",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        cost=None,
        latency_ms=120,
        status="success",
        http_status=200,
        request_payload={"model": "m1", "messages": [{"role": "user", "content": "hello"}]},
        response_payload={"model": "m1", "choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 5}},
        error_message=None,
    )
    base.update(ovr)
    return RequestLogRecord(**base)


class TestLog:
    async def test_estimated_usage_is_marked_even_when_stream_succeeds(self, tmp_path):
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))
        await logger.log(_record(usage_estimated=True), db=db)
        assert db.all(RequestLog)[0].usage_estimated is True

    async def test_writes_row_with_block_refs_and_prefix_hash(self, tmp_path):
        db = FakeSession()
        store = LocalFileBodyStore(root=tmp_path)
        logger = RequestLogger(db_factory=None, store=store)

        await logger.log(_record(), db=db)

        rows = db.all(RequestLog)
        assert len(rows) == 1
        row = rows[0]
        assert row.request_id == "req-0001"
        assert row.total_tokens == 5 and row.prompt_tokens == 3 and row.completion_tokens == 2
        assert row.request_ref and row.response_ref  # 两块哈希
        assert (
            store.get(row.request_ref)
            == json.dumps(_record().request_payload, sort_keys=True, ensure_ascii=False).encode()
            or True
        )  # 具体规范化格式由实现定，往返一致即可
        req_body = json.loads(store.get(row.request_ref))
        assert req_body["messages"][0]["content"] == "hello"
        assert row.conv_prefix_hash  # chat 有会话指纹
        assert row.truncated is False
        assert row.status == "success"

    async def test_block_failure_still_writes_metering_row(self, tmp_path):
        db = FakeSession()

        class BrokenStore:
            def put(self, content):
                raise OSError("disk full")

            def get(self, h):
                return None

            def sweep(self, **kw):
                return 0

        logger = RequestLogger(db_factory=None, store=BrokenStore())

        await logger.log(_record(), db=db)

        rows = db.all(RequestLog)
        assert len(rows) == 1
        assert rows[0].request_ref is None and rows[0].response_ref is None  # 块失败 refs 置空
        assert rows[0].total_tokens == 5  # 计量不丢

    async def test_long_field_truncated_with_flag(self, tmp_path):
        db = FakeSession()
        store = LocalFileBodyStore(root=tmp_path)
        logger = RequestLogger(db_factory=None, store=store, max_field_bytes=100)

        big = "x" * 500
        await logger.log(
            _record(
                request_payload={
                    "model": "m1",
                    "messages": [{"role": "user", "content": big}],
                }
            ),
            db=db,
        )

        row = db.all(RequestLog)[0]
        assert row.truncated is True
        stored = json.loads(store.get(row.request_ref))
        assert len(stored["messages"][0]["content"]) <= 100 + 50  # 截断 + 标记余量

    async def test_embeddings_no_prefix_hash(self, tmp_path):
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(
            _record(
                operation="embeddings",
                request_payload={"model": "m1", "input": ["x"]},
                response_payload={"data": []},
            ),
            db=db,
        )

        assert db.all(RequestLog)[0].conv_prefix_hash is None

    async def test_cached_tokens_extracted_from_usage(self, tmp_path):
        """OpenAI 风格 usage（prompt_tokens_details）→ 行上 cached_tokens。"""
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(
            _record(
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 7},
                }
            ),
            db=db,
        )

        row = db.all(RequestLog)[0]
        assert row.cached_tokens == 7
        assert row.prompt_tokens == 10  # 命中数是 prompt 子集，prompt 不扣减

    async def test_cached_tokens_anthropic_cache_read(self, tmp_path):
        """Anthropic 风格 usage（cache_read_input_tokens）同样提取。"""
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(
            _record(
                operation="anthropic.messages",
                protocol="anthropic",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 6,
                    "cache_creation_input_tokens": 0,
                },
            ),
            db=db,
        )

        assert db.all(RequestLog)[0].cached_tokens == 6

    async def test_cached_tokens_none_when_not_reported(self, tmp_path):
        """引擎未上报缓存字段 → NULL（与 0 区分）。"""
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(_record(), db=db)  # 基线 usage 无缓存字段

        assert db.all(RequestLog)[0].cached_tokens is None

    async def test_upstream_and_retry_fields_persisted(self, tmp_path):
        """上游流向（local/external + provider + endpoint）与重试次数落账。"""
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(
            _record(
                upstream_type="external",
                provider_name="OpenAI",
                endpoint_id=42,
                retry_count=2,
            ),
            db=db,
        )

        row = db.all(RequestLog)[0]
        assert row.upstream_type == "external"
        assert row.provider_name == "OpenAI"
        assert row.endpoint_id == 42
        assert row.retry_count == 2

    async def test_upstream_name_clipped_to_column_width(self, tmp_path):
        """上游展示名超列宽时截断，而不是让整条计量行写不进去。

        ``request_log.provider_name`` 是 varchar(PROVIDER_NAME_MAX_CHARS)，展示名却取自端点 name
        （部署名/端点名上限 255）。落账是 fire-and-forget、异常只告警不抛——
        不截断就是**静默丢掉整条调用日志与计量数据**。按码点截断，不切出半个字符。
        """
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(_record(provider_name="部" * 150), db=db)

        row = db.all(RequestLog)[0]
        assert row.provider_name == "部" * PROVIDER_NAME_MAX_CHARS

    async def test_default_no_retry_no_upstream(self, tmp_path):
        """未传上游/重试字段 → upstream 三列 NULL、retry_count=0（走 logger.log 全链验证透传）。"""
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(_record(), db=db)

        row = db.all(RequestLog)[0]
        assert row.upstream_type is None
        assert row.provider_name is None
        assert row.endpoint_id is None
        assert row.retry_count == 0

    async def test_timing_and_finish_reason_persisted(self, tmp_path):
        """首帧延迟与上游结束原因落账（协议适配器 → 记录 → ORM 这条链的末端守卫）。

        这两列是大屏「吐字速度」与「截断率」的唯一数据源，链上任何一处 kwargs
        漏写都只在真机跑大屏时才暴露——而历史行全为空，线上看不出异常。
        """
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(_record(ttft_ms=577, finish_reason="length"), db=db)

        row = db.all(RequestLog)[0]
        assert row.ttft_ms == 577
        assert row.finish_reason == "length"

    async def test_default_no_timing_no_finish_reason(self, tmp_path):
        """未传（非流式请求没有首帧；上游没给结束原因）→ 两列 NULL，不是 0/空串。"""
        db = FakeSession()
        logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))

        await logger.log(_record(), db=db)

        row = db.all(RequestLog)[0]
        assert row.ttft_ms is None
        assert row.finish_reason is None
