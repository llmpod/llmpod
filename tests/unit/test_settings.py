"""Settings 配置单元测试。"""

from __future__ import annotations

from llmpod.config.settings import DEFAULT_OFFICIAL_IMAGE, Settings


class TestSettings:
    def test_default_values(self):
        """测试默认值已设置（忽略 .env 覆盖）。"""
        # 通过指定不存在的文件来避免加载 .env
        s = Settings(
            _env_file=None,
            pg_host="test",
            pg_password="test",
        )
        assert s.pg_port == 5432
        assert s.pg_user == "llmpod"
        assert s.pg_database == "llmpod"
        assert s.redis_db == 0
        assert s.webserver_port == 8080
        assert s.proxy_port == 8090
        assert s.node_heartbeat_interval == 10

    def test_pg_url(self):
        """测试 PostgreSQL URL 生成。"""
        s = Settings(
            _env_file=None,
            pg_host="db.local",
            pg_port=5433,
            pg_user="testuser",
            pg_password="testpass",
            pg_database="testdb",
        )
        assert s.pg_url == "postgresql+asyncpg://testuser:testpass@db.local:5433/testdb"

    def test_redis_url_with_password(self):
        """测试带密码的 Redis URL。"""
        s = Settings(
            _env_file=None,
            redis_host="redis.local",
            redis_port=6380,
            redis_password="secret",
            redis_db=2,
        )
        assert s.redis_url == "redis://:secret@redis.local:6380/2"

    def test_redis_url_without_password(self):
        """测试不带密码的 Redis URL。"""
        s = Settings(
            _env_file=None,
            redis_host="redis.local",
            redis_port=6379,
            redis_password="",
            redis_db=0,
        )
        assert s.redis_url == "redis://redis.local:6379/0"

    def test_env_prefix(self):
        """测试配置使用 LLMPOD_ 前缀。"""
        assert Settings.model_config.get("env_prefix") == "LLMPOD_"

    def test_env_file(self):
        """测试配置读取 .env 文件。"""
        assert Settings.model_config.get("env_file") == ".env"

    def test_node_defaults(self):
        """测试节点相关的默认配置。"""
        s = Settings(
            _env_file=None,
        )
        assert s.node_id == ""
        assert s.node_cluster_id == 0
        assert s.node_token == ""
        assert s.node_name == ""
        assert s.node_reconnect_fast_interval == 5
        assert s.node_reconnect_slow_interval == 30

    def test_node_name_from_environment(self, monkeypatch):
        monkeypatch.setenv("LLMPOD_NODE_NAME", "gpu-west-2")
        assert Settings(_env_file=None).node_name == "gpu-west-2"

    def test_legacy_scrape_interval_backfills_engine_tiers(self):
        """旧键只回退引擎两档（proxy 档语义已变，不吃旧值）。"""
        s = Settings(_env_file=None, metrics_scrape_interval=15)
        assert s.metrics_scrape_interval_proxy == 5
        assert s.metrics_scrape_interval_engine_fast == 15
        assert s.metrics_scrape_interval_engine_slow == 15

    def test_legacy_interval_does_not_override_explicit_tier(self):
        """回归：旧键不抬高显式配置的新档——旧实现 legacy 回填 fast 后经
        slow≥fast 钳制，把显式 ENGINE_SLOW=30 静默抬到 legacy=60。"""
        s = Settings(
            _env_file=None,
            metrics_scrape_interval=60,
            metrics_scrape_interval_engine_slow=30,
        )
        assert s.metrics_scrape_interval_engine_fast == 5  # 未显式配置 → 默认
        assert s.metrics_scrape_interval_engine_slow == 30  # 显式值不被旧键抬起

    def test_explicit_tiers_win_over_legacy(self):
        """两档都显式配置时旧键完全不参与。"""
        s = Settings(
            _env_file=None,
            metrics_scrape_interval=60,
            metrics_scrape_interval_engine_fast=5,
            metrics_scrape_interval_engine_slow=30,
        )
        assert (s.metrics_scrape_interval_engine_fast, s.metrics_scrape_interval_engine_slow) == (5, 30)

    def test_explicit_slow_below_fast_clamped(self):
        """显式 slow < 显式 fast 属配置写反，钳到 fast（宁可多抓）。"""
        s = Settings(
            _env_file=None,
            metrics_scrape_interval_engine_fast=30,
            metrics_scrape_interval_engine_slow=5,
        )
        assert s.metrics_scrape_interval_engine_slow == 30

    def test_official_image_default_and_override(self):
        """官方镜像仓库可配；空串（compose 透传）回落到默认。"""
        assert Settings(_env_file=None).official_image == DEFAULT_OFFICIAL_IMAGE
        assert Settings(_env_file=None, official_image="").official_image == DEFAULT_OFFICIAL_IMAGE
        assert (
            Settings(_env_file=None, official_image=" registry.local/llmpod ").official_image == "registry.local/llmpod"
        )
        assert (
            Settings(_env_file=None, official_image="registry.local/llmpod/").official_image == "registry.local/llmpod"
        )
