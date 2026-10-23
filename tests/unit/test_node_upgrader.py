"""Node 自升级执行器单测：分类/预检/回滚/维护回调/boot 恢复收敛。

docker SDK 全部 monkeypatch（FakeDockerClient 替身），覆盖升级替换流程的分支语义：
new_id 铁律（回滚只认 id）、pull 分类、ready 超时、崩溃早判、快照恢复、
兜底不动、boot 恢复各相位收敛。
"""

from __future__ import annotations

import asyncio
import json

import pytest

import llmpod
from llmpod.apps.node import upgrader as up
from llmpod.config.settings import Settings


# ---- Fake docker 对象 ----


class FakeContainer:
    """替身容器：remove 打标记不真删（便于断言）；attrs 形状对齐 docker inspect。"""

    def __init__(self, name: str, status: str = "running", mounts=None, attrs=None, cid: str | None = None):
        self.name = name
        self.id = cid or f"id-{name}"
        self.status = status
        self.removed = False
        self.attrs = attrs or {
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "Config": {"Env": ["LLMPOD_NODE_IMAGE=old:1", "FOO=bar"], "Labels": {"a": "b"}},
            "Mounts": mounts or [{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"}],
            "State": {"ExitCode": 7},
        }

    def rename(self, new_name):
        self.name = new_name

    def reload(self):
        pass

    def start(self):
        self.status = "running"

    def remove(self, force=False):
        self.removed = True

    def logs(self, tail=10):
        return b"line1\nline2"


class FakeDockerClient:
    """容器按 name/id 双键查；create 走 api.create_container（与真实回放路径一致）。"""

    def __init__(self):
        self._containers: list[FakeContainer] = []
        self.images_local: set[str] = set()
        self.pull_error: Exception | None = None
        self.create_error: Exception | None = None
        self.created: list[dict] = []
        self.volumes_removed: list[str] = []

    def add(self, container: FakeContainer) -> FakeContainer:
        self._containers.append(container)
        return container

    def _get(self, ref: str) -> FakeContainer:
        for c in self._containers:
            if not c.removed and (c.name == ref or c.id == ref):
                return c
        from docker.errors import NotFound

        raise NotFound(f"no such container {ref}")

    def find(self, ref: str) -> FakeContainer | None:
        """含已 removed 的查找（断言「被删」用）；活容器查找走 _get。"""
        for c in self._containers:
            if c.name == ref or c.id == ref:
                return c
        return None

    @property
    def all_containers(self) -> list[FakeContainer]:
        return [c for c in self._containers if not c.removed]

    @property
    def containers(self):
        cli = self

        class _Containers:
            def get(self, name):
                return cli._get(name)

            def list(self, all=False, filters=None):
                # all=False 只看 running（对齐 docker 语义，all=True 含已停止）
                items = [c for c in cli.all_containers if all or c.status == "running"]
                if filters and filters.get("label"):
                    wanted = filters["label"]
                    if isinstance(wanted, str):
                        wanted = [wanted]
                    for cond in wanted:
                        key, _, val = cond.partition("=")
                        items = [c for c in items if (c.attrs["Config"].get("Labels") or {}).get(key) == val]
                if filters and filters.get("volume"):
                    vol = filters["volume"]
                    items = [
                        c
                        for c in items
                        if any(m.get("Name") == vol or m.get("Source") == vol for m in c.attrs.get("Mounts") or [])
                    ]
                return items

            def run(self, image, command=None, *, name=None, **kw):
                cli.created.append({"name": name, "image": image, "command": command, **kw})
                container = FakeContainer(name or f"run-{len(cli.created)}")
                container.attrs["Config"]["Labels"] = dict(kw.get("labels") or {})
                cli.add(container)
                return container

        return _Containers()

    @property
    def volumes(self):
        cli = self

        class _Volumes:
            def get(self, name):
                class _Volume:
                    def remove(self):
                        cli.volumes_removed.append(name)

                return _Volume()

        return _Volumes()

    @property
    def images(self):
        cli = self

        class _Images:
            def get(self, image):
                if image not in cli.images_local:
                    from docker.errors import ImageNotFound

                    raise ImageNotFound(f"no such image {image}")
                return object()

            def pull(self, image):
                if cli.pull_error is not None:
                    raise cli.pull_error
                cli.images_local.add(image)

        return _Images()

    @property
    def api(self):
        cli = self

        class _Api:
            def create_container(self, *, name, image, **kw):
                if cli.create_error is not None:
                    raise cli.create_error
                cli.created.append({"name": name, "image": image, **kw})
                container = FakeContainer(
                    name,
                    cid=f"new-id-{name}",
                    mounts=[{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"}],
                )
                container.attrs["Config"]["Env"] = kw.get("environment") or []
                cli.add(container)
                return {"Id": container.id}

        return _Api()


# ---- fixtures ----


@pytest.fixture
def env(tmp_path, monkeypatch):
    """node 容器 env + 数据卷布局（node_task_db 在数据卷内）。"""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "node_tasks.db"
    db.write_bytes(b"sqlite-bytes")
    monkeypatch.setenv("LLMPOD_NODE_CONTAINER_NAME", "llmpod-node")
    settings = Settings(node_task_db=str(db))
    return {"data": data, "db": db, "settings": settings}


@pytest.fixture
def no_exit(monkeypatch):
    """拦截 os._exit（成功 rm -f self 与自清理路径的硬退出）。

    stub 抛 SystemExit 模拟真实语义（进程终止、后续代码不执行）——若被 except
    Exception 误吞会在这里暴露（SystemExit 是 BaseException，不会被吞）。
    """
    exits: list[int] = []

    def _fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(up.os, "_exit", _fake_exit)
    return exits


@pytest.fixture
def agent():
    calls = []

    async def suspend():
        calls.append("suspend")

    async def resume():
        calls.append("resume")

    return calls, suspend, resume


def _make_upgrader(env, agent, cli=None, monkeypatch=None):
    calls, suspend, resume = agent
    u = up.NodeUpgrader(env["settings"], suspend=suspend, resume=resume)
    if cli is not None and monkeypatch is not None:
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
    return u, calls


# ---- 分类与预检 ----


class TestClassify:
    def test_auth(self):
        assert up.classify_pull_error(Exception("pull access denied to repo")) == "auth_failure"
        assert up.classify_pull_error(Exception("401 Unauthorized")) == "auth_failure"

    def test_not_found(self):
        assert up.classify_pull_error(Exception("image not found: manifest unknown")) == "image_not_found"

    def test_mirror_resolve_reference(self):
        """国内加速器（daocloud 等）对不存在镜像报 resolve reference，应归 image_not_found。"""
        msg = (
            '500 Server Error ... Internal Server Error ("unknown: failed to resolve '
            'reference "docker.io/btpanel/llmpod:26.9.98": unexpected status from HEAD request'
        )
        assert up.classify_pull_error(Exception(msg)) == "image_not_found"

    def test_network(self):
        assert up.classify_pull_error(Exception("connection timed out")) == "network_error"

    def test_unknown(self):
        assert up.classify_pull_error(Exception("weird")) == "unknown"

    def test_registry_ref(self):
        assert up.is_registry_ref("btpanel/llmpod:26.9.3") is True
        assert up.is_registry_ref("registry.local:5000/ns/app:1") is True
        assert up.is_registry_ref("llmpod:latest") is False


class TestPrecheck:
    async def test_missing_container_name_not_supported(self, env, agent, monkeypatch):
        monkeypatch.delenv("LLMPOD_NODE_CONTAINER_NAME")
        u, calls = _make_upgrader(env, agent)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "not_supported"
        assert calls == []  # 未进维护

    async def test_local_tag_not_supported(self, env, agent):
        u, calls = _make_upgrader(env, agent)
        out = await u._dance("99.0.0", "llmpod:latest")
        assert out.result == "not_supported"
        assert "save/load" in (out.detail or "")

    async def test_target_not_newer_ignored(self, env, agent):
        """target ≤ 自身版本 → 忽略（None = 无需上报）。"""
        u, _ = _make_upgrader(env, agent)
        assert await u._dance(llmpod.__version__, "reg.local/a:1") is None

    async def test_duplicate_proposal_ignored(self, env, agent, monkeypatch):
        """升级进行中（_busy）时第二个提议被忽略（不重跑 _dance）。"""
        u, _ = _make_upgrader(env, agent)
        u._busy = True
        called: list = []

        async def fake_dance(target, image_ref):
            called.append((target, image_ref))
            return None

        monkeypatch.setattr(u, "_dance", fake_dance)
        await u.handle_upgrade({"target_version": "99.0.0", "image_ref": "reg.local/a:1"})
        assert called == []  # 重复提议被忽略，未触发升级替换流程

    async def test_stale_old_running_aborts(self, env, agent, monkeypatch):
        """残留 -old 在跑且持有者是异容器 → 不敢动，not_supported。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node"))
        cli.add(FakeContainer("llmpod-node-old", status="running"))
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "not_supported"
        assert "人工" in (out.detail or "")

    async def test_stale_old_running_is_self_guides_restart(self, env, agent, monkeypatch):
        """-old 的 running 持有者就是本进程（上次中断后自己活在 -old 名下）→
        不能建议 docker rm（那删掉的是活节点），指引 restart 交启动恢复收敛。"""
        cli = FakeDockerClient()
        me = cli.add(FakeContainer("llmpod-node-old", status="running", cid=_MY_ID))
        cli.add(FakeContainer("llmpod-node", cid=_HOLDER_ID))  # 接管者占住原名
        u, _ = _make_upgrader(env, agent, cli, monkeypatch)
        monkeypatch.setattr(up, "_self_container_id", lambda cli=None: _MY_ID)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "not_supported"
        assert "restart" in (out.detail or "")
        assert me.removed is False


# ---- 升级替换流程分支 ----


class TestDance:
    def _base_cli(self):
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node"))
        cli.images_local.add("reg.local/a:1")
        return cli

    async def test_pull_failed_classification(self, env, agent, monkeypatch):
        """pull 失败分类上报；容器不动、维护退出。"""
        cli = self._base_cli()
        cli.images_local.clear()
        cli.pull_error = Exception("pull access denied")
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "pull_failed" and out.error_class == "auth_failure"
        assert cli.find("llmpod-node").name == "llmpod-node"  # 未 rename
        assert calls == ["suspend", "resume"]
        assert not (env["data"] / up.STATE_FILE).exists()
        assert not u._snapshot_path().exists()  # 快照已弃

    async def test_success_path_self_removes(self, env, agent, monkeypatch, no_exit):
        """成功路径：ready marker → 清状态 → rm -f 自己 → 硬退出。"""
        cli = self._base_cli()
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)

        async def fake_wait(new, t0, target="", token=""):
            u.write_ready_marker("node-x")  # 模拟新容器写 marker
            return True, None, None

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        with pytest.raises(SystemExit):
            await u._dance("99.0.0", "reg.local/a:1")
        # 老容器被 rename 到 -old 后 rm -f self（removed 标记）
        old = [c for c in cli._containers if c.id == "id-llmpod-node"][0]
        assert old.name == "llmpod-node-old" and old.removed is True
        assert no_exit == [0]
        assert not (env["data"] / up.STATE_FILE).exists()
        assert not u._snapshot_path().exists()

    async def test_ready_timeout_rollback(self, env, agent, monkeypatch):
        """残留新容器：ready 超时 → rm 新容器(按 new_id) + 快照恢复 + rename 回原名。"""
        cli = self._base_cli()
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)

        async def fake_wait(new, t0, target="", token=""):
            return False, "ready_timeout", "zombie logs"

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "not_ready" and "zombie logs" in (out.detail or "")
        # 新容器被删、老容器 rename 回原名
        assert cli.find("new-id-llmpod-node").removed is True
        old = cli.find("id-llmpod-node")
        assert old.name == "llmpod-node" and not old.removed
        # 快照恢复：库文件回到原字节，bak 清除
        assert env["db"].read_bytes() == b"sqlite-bytes"
        assert not u._snapshot_path().exists()
        assert calls == ["suspend", "resume"]
        assert not (env["data"] / up.STATE_FILE).exists()

    async def test_crashed_early_detect(self, env, agent, monkeypatch):
        """新容器 exiting/restarting 早判（不等满 5 分钟）。"""
        cli = self._base_cli()
        u, _ = _make_upgrader(env, agent, cli, monkeypatch)

        async def fake_wait(new, t0, target="", token=""):
            return False, "crashed(status=restarting, exit=7)", "boom"

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "start_failed"
        assert "crashed" in (out.error_class or "")

    async def test_create_failed_rollback(self, env, agent, monkeypatch):
        """create 失败：无新容器 → rename 回原名 + create_failed。"""
        cli = self._base_cli()
        cli.create_error = RuntimeError("name conflict")
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "create_failed"
        assert cli.find("id-llmpod-node").name == "llmpod-node"
        assert calls == ["suspend", "resume"]
        assert not (env["data"] / up.STATE_FILE).exists()

    async def test_unexpected_error_touches_nothing(self, env, agent, monkeypatch):
        """兜底铁律：未预期异常不动容器、恢复维护、落盘相位、上报 unknown_error。"""
        cli = self._base_cli()
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)

        async def boom(*a, **kw):
            raise RuntimeError("surprise")

        monkeypatch.setattr(u, "_rollback", boom)

        async def fake_wait(new, t0, target="", token=""):
            return False, "ready_timeout", None

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "unknown_error"
        state = json.loads((env["data"] / up.STATE_FILE).read_text())
        assert state["phase"].startswith("error:")
        assert calls == ["suspend", "resume"]

    async def test_env_overwritten(self, env, agent, monkeypatch, no_exit):
        """create 的 Env：镜像 env 覆写 + 升级令牌注入，其余原样。"""
        cli = self._base_cli()
        u, _ = _make_upgrader(env, agent, cli, monkeypatch)

        async def fake_wait(new, t0, target="", token=""):
            u.write_ready_marker("n")
            return True, None, None

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        with pytest.raises(SystemExit):
            await u._dance("99.0.0", "reg.local/a:1")
        env_list = cli.created[0]["environment"]
        assert "LLMPOD_NODE_IMAGE=reg.local/a:1" in env_list
        assert not any(e.startswith("LLMPOD_NODE_IMAGE=old") for e in env_list)
        assert "FOO=bar" in env_list
        assert any(e.startswith(up.DANCE_TOKEN_ENV + "=") for e in env_list)

    async def test_healthcheck_replayed(self, env, agent, monkeypatch, no_exit):
        """create 回放带 Config.Healthcheck：引导脚本 --no-healthcheck 的
        Test:["NONE"] 不丢——漏传会让新容器回退镜像内置探活（node 无 HTTP，
        永久 unhealthy）。"""
        cli = self._base_cli()
        cli.find("llmpod-node").attrs["Config"]["Healthcheck"] = {"Test": ["NONE"]}
        u, _ = _make_upgrader(env, agent, cli, monkeypatch)

        async def fake_wait(new, t0, target="", token=""):
            u.write_ready_marker("n")
            return True, None, None

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        with pytest.raises(SystemExit):
            await u._dance("99.0.0", "reg.local/a:1")
        assert cli.created[0]["healthcheck"] == {"Test": ["NONE"]}

    async def test_mount_assertion_failure_rolls_back(self, env, agent, monkeypatch):
        """挂载断言不一致 → create_failed 回滚（新容器按 new_id 删、rename 回原名）。"""
        cli = self._base_cli()
        cli.find("llmpod-node").attrs["Mounts"].append({"Source": "data-vol", "Destination": "/app/data"})
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "create_failed"
        assert out.error_class == "mount_replay_mismatch"
        assert cli.find("id-llmpod-node").name == "llmpod-node"  # rename 回原名
        assert cli.find("new-id-llmpod-node").removed is True
        assert calls == ["suspend", "resume"]

    async def test_rollback_incomplete_keeps_state_no_resume(self, env, agent, monkeypatch):
        """回滚三步任一步失败 → 保留状态文件与快照、留在维护态（不 resume）——
        清了状态 = 毁掉 boot 恢复的唯一入口；resume 继续写库会让下次启动的
        快照还原丢掉这段写入。上报附 docker restart 指引。"""
        cli = self._base_cli()
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)

        def broken_snapshot():
            raise OSError("disk full")

        monkeypatch.setattr(u, "_restore_snapshot", broken_snapshot)

        async def fake_wait(new, t0, target="", token=""):
            return False, "ready_timeout", None

        monkeypatch.setattr(u, "_wait_ready", fake_wait)
        out = await u._dance("99.0.0", "reg.local/a:1")
        assert out.result == "not_ready"
        assert "docker restart" in (out.detail or "")
        assert out.error_class == "ready_timeout"  # 根因保留，不被回滚未完成覆盖
        # 状态与快照保留（boot 恢复收敛的输入）、维护未退出
        assert (env["data"] / up.STATE_FILE).exists()
        assert u._snapshot_path().exists()
        assert calls == ["suspend"]
        # 已完成的回滚步骤照做：新容器删了、老容器 rename 回原名
        assert cli.find("new-id-llmpod-node").removed is True
        assert cli.find("id-llmpod-node").name == "llmpod-node"

    async def test_success_rm_failure_still_exits(self, env, agent, monkeypatch, no_exit):
        """成功路径 rm -f 自己失败也要硬退出：落兜底会让老容器退出维护、与新
        容器双活共写同一数据卷。退出后 restart 拉起撞 retire_if_superseded 收敛。"""
        cli = self._base_cli()
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)

        async def fake_wait(new, t0, target="", token=""):
            u.write_ready_marker("node-x")
            return True, None, None

        monkeypatch.setattr(u, "_wait_ready", fake_wait)

        def fail_remove(force=False):
            raise RuntimeError("daemon busy")

        old = cli.find("llmpod-node")
        monkeypatch.setattr(old, "remove", fail_remove)
        with pytest.raises(SystemExit):
            await u._dance("99.0.0", "reg.local/a:1")
        assert no_exit == [0]
        assert old.name == "llmpod-node-old" and old.removed is False
        assert calls == ["suspend"]  # 没有.resume（兜底不再触发）


# ---- ready marker / 状态文件 ----


class TestMarkers:
    def test_write_ready_marker(self, env):
        u = up.NodeUpgrader(env["settings"])
        u.write_ready_marker("node-x")
        marker = json.loads((env["data"] / up.READY_FILE).read_text())
        assert marker["version"] == llmpod.__version__
        assert marker["node_id"] == "node-x"
        assert marker["ts"] > 0

    async def test_wait_ready_reads_marker(self, env, monkeypatch):
        """marker ts > t0 且 version 非空 → 就绪；旧 marker（ts ≤ t0）不算。"""
        u = up.NodeUpgrader(env["settings"])
        new = FakeContainer("new")
        # 旧 marker（ts 很小）
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "1", "ts": 1}))

        async def fast_sleep(_):
            return None

        monkeypatch.setattr(up.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(up, "READY_TIMEOUT_SECONDS", 0.05)
        ready, ec, _ = await u._wait_ready(new, t0=1000.0)
        assert ready is False and ec == "ready_timeout"

        # 新 marker（ts 晚于 t0）
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "9", "ts": 2000.0}))
        ready, ec, _ = await u._wait_ready(new, t0=1000.0)
        assert ready is True

    async def test_wait_ready_crashed_status(self, env):
        """新容器 restarting → 立即失败（不等超时）。"""
        u = up.NodeUpgrader(env["settings"])
        new = FakeContainer("new", status="restarting")
        ready, ec, logs = await u._wait_ready(new, t0=0.0)
        assert ready is False
        assert ec and ec.startswith("crashed")
        assert logs == "line1\nline2"

    async def test_wait_ready_version_mismatch(self, env):
        """marker 版本未追平 target（本地同名 tag 的旧镜像）→ 判失败交回滚，
        不让「升级成功」名不副实。"""
        u = up.NodeUpgrader(env["settings"])
        new = FakeContainer("new")
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "26.9.1", "ts": 2000.0}))
        ready, ec, _ = await u._wait_ready(new, t0=1000.0, target_version="26.9.26")
        assert ready is False
        assert ec and ec.startswith("version_mismatch")

    async def test_wait_ready_version_reached(self, env):
        """marker 版本 ≥ target（相等）→ 就绪。"""
        u = up.NodeUpgrader(env["settings"])
        new = FakeContainer("new")
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "26.9.26", "ts": 2000.0}))
        ready, ec, _ = await u._wait_ready(new, t0=1000.0, target_version="26.9.26")
        assert ready is True and ec is None

    async def test_wait_ready_ignores_foreign_marker(self, env, monkeypatch):
        """升级窗口内老容器 WS 抖线重连也会写 marker（旧版本+新鲜 ts）——不带本次
        升级令牌的一律忽略继续等，绝不据此误判 version_mismatch 回滚健康新容器。"""
        u = up.NodeUpgrader(env["settings"])
        new = FakeContainer("new")

        async def fast_sleep(_):
            return None

        monkeypatch.setattr(up.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(up, "READY_TIMEOUT_SECONDS", 0.05)
        # 老容器写的：无 dance 字段（或他人令牌），ts 新鲜、版本旧
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "26.9.1", "ts": 2000.0}))
        ready, ec, _ = await u._wait_ready(new, t0=1000.0, target_version="26.9.26", token="tok-9")
        assert ready is False and ec == "ready_timeout"  # 被忽略，不是 version_mismatch

        # 本次升级新容器写的：令牌匹配 → 正常就绪
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "26.9.26", "ts": 2000.0, "dance": "tok-9"}))
        ready, ec, _ = await u._wait_ready(new, t0=1000.0, target_version="26.9.26", token="tok-9")
        assert ready is True and ec is None

    def test_write_ready_marker_carries_dance(self, env, monkeypatch):
        """升级新容器（env 带升级令牌）写的 marker 附 dance；无令牌（首代节点/
        老容器）不附——轮询侧据此甄别写出者。"""
        monkeypatch.setenv(up.DANCE_TOKEN_ENV, "tok-3")
        u = up.NodeUpgrader(env["settings"])
        u.write_ready_marker("n")
        marker = json.loads((env["data"] / up.READY_FILE).read_text())
        assert marker["dance"] == "tok-3"

        monkeypatch.delenv(up.DANCE_TOKEN_ENV)
        u.write_ready_marker("n")
        marker = json.loads((env["data"] / up.READY_FILE).read_text())
        assert "dance" not in marker


# ---- boot 恢复 ----


class TestBootRecovery:
    async def test_no_state_normal_boot(self, env):
        u = up.NodeUpgrader(env["settings"])
        assert await u.recover_on_boot() is False

    async def test_new_container_token_match(self, env, monkeypatch):
        """升级令牌匹配（我是新容器）→ 清状态文件但**保留快照**（留给旧容器确认后处置）。"""
        state = {"phase": "started", "target": "9", "image_ref": "r", "new_id": "abc", "token": "tok-1", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        monkeypatch.setenv(up.DANCE_TOKEN_ENV, "tok-1")
        u = up.NodeUpgrader(env["settings"])
        u._make_snapshot()
        assert u._snapshot_path().exists()
        assert await u.recover_on_boot() is False
        assert not (env["data"] / up.STATE_FILE).exists()
        # 快照保留：新容器若在写 ready marker 前崩溃，旧容器回滚恢复靠它（勿过早 discard）
        assert u._snapshot_path().exists()

    async def test_old_container_renamed_phase(self, env, monkeypatch):
        """renamed 相位（create 前死）：老容器在 -old 名下 → rename 回原名；
        从未建出新容器 → 快照只弃不还原（还原会拿替换流程开始时的旧快照盖掉
        此后正常运行的写入）。"""
        cli = FakeDockerClient()
        old = cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        _wire_self_id(monkeypatch, _MY_ID)
        u = up.NodeUpgrader(env["settings"])
        env["db"].write_bytes(b"snapshot-bytes")
        u._make_snapshot()
        env["db"].write_bytes(b"current-bytes")
        state = {"phase": "renamed", "target": "9", "image_ref": "r", "new_id": None, "token": "tok-1", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        assert await u.recover_on_boot() is True
        assert old.name == "llmpod-node"
        assert env["db"].read_bytes() == b"current-bytes"  # 未被旧快照覆盖
        assert not u._snapshot_path().exists()
        assert not (env["data"] / up.STATE_FILE).exists()

    async def test_old_container_started_phase_halfmade(self, env, monkeypatch):
        """started 相位 + 新容器未就绪 → rm 半成品（按落盘 new_id）+ 回家。"""
        cli = FakeDockerClient()
        old = cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        _wire_self_id(monkeypatch, _MY_ID)
        half = cli.add(FakeContainer("llmpod-node", cid="new-id-llmpod-node"))  # 半成品占原名
        u = up.NodeUpgrader(env["settings"])
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        monkeypatch.setattr(up, "READY_TIMEOUT_SECONDS", 0.05)
        state = {"phase": "started", "target": "9", "image_ref": "r", "new_id": half.id, "token": "tok-1", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        assert await u.recover_on_boot() is True
        assert half.removed is True
        assert old.name == "llmpod-node"
        assert not (env["data"] / up.STATE_FILE).exists()

    async def test_old_container_new_ready_self_removes(self, env, monkeypatch, no_exit):
        """started 相位 + 新容器已写 fresh marker → 老容器功成身退（rm -f self）。"""
        cli = FakeDockerClient()
        old = cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        new = cli.add(FakeContainer("llmpod-node", cid="new-id-llmpod-node"))
        _wire_self_id(monkeypatch, _MY_ID)
        u = up.NodeUpgrader(env["settings"])
        import time as _time

        # 新容器写的 marker：带升级令牌（写出者甄别只认本次升级新容器）
        (env["data"] / up.READY_FILE).write_text(json.dumps({"version": "9", "ts": _time.time(), "dance": "tok-1"}))
        state = {
            "phase": "started",
            "target": "9",
            "image_ref": "r",
            "new_id": new.id,
            "token": "tok-1",
            "ts": _time.time() - 10,
        }
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await u.recover_on_boot()
        assert old.removed is True
        assert new.removed is False
        assert no_exit == [0]

    async def test_stale_state_from_previous_node_only_cleared(self, env, monkeypatch):
        """前任节点漏清的状态（token 不匹配 + 名下都不是自己，典型=复用数据卷重装）
        → 只清文件：不回放前任快照（新节点任务库原样保留）、不做任何 docker 操作。"""
        cli = FakeDockerClient()
        stranger_old = cli.add(FakeContainer("llmpod-node-old", cid=_HOLDER_ID))  # 前任替换流程残迹
        stranger_new = cli.add(FakeContainer("llmpod-node", cid="new-id-llmpod-node"))
        _wire_self_id(monkeypatch, _MY_ID)  # 本容器（新节点）不在任何名字下
        u = up.NodeUpgrader(env["settings"])
        env["db"].write_bytes(b"fresh-node-db")
        u._make_snapshot()  # 前任快照残留
        state = {
            "phase": "started",
            "target": "9",
            "image_ref": "r",
            "new_id": stranger_new.id,
            "token": "tok-1",
            "ts": 1.0,
        }
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        assert await u.recover_on_boot() is False
        assert env["db"].read_bytes() == b"fresh-node-db"  # 未被前任快照覆盖
        assert not (env["data"] / up.STATE_FILE).exists()
        assert not u._snapshot_path().exists()
        assert not stranger_old.removed and not stranger_new.removed  # 前任容器不动

    async def test_missing_container_name_keeps_state(self, env, monkeypatch):
        """缺容器名的存量节点：无法收敛，保留状态文件返回 True。"""
        monkeypatch.delenv("LLMPOD_NODE_CONTAINER_NAME")
        state = {"phase": "started", "target": "9", "image_ref": "r", "new_id": "x", "token": "t", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        u = up.NodeUpgrader(env["settings"])
        assert await u.recover_on_boot() is True
        assert (env["data"] / up.STATE_FILE).exists()

    async def test_corrupt_state_file_removed(self, env):
        (env["data"] / up.STATE_FILE).write_text("not json")
        u = up.NodeUpgrader(env["settings"])
        assert await u.recover_on_boot() is False
        assert not (env["data"] / up.STATE_FILE).exists()

    async def test_creating_phase_clears_own_halfmade(self, env, monkeypatch):
        """creating 相位（create 进行中被打断）：{name} 被带本次升级令牌的半成品
        占住 → 清它 + rename 回原名；半成品从未被 start，快照只弃不还原。"""
        cli = FakeDockerClient()
        old = cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        half = FakeContainer("llmpod-node", cid="half-made-id")
        half.attrs["Config"]["Env"] = [f"{up.DANCE_TOKEN_ENV}=tok-1"]
        cli.add(half)
        _wire_self_id(monkeypatch, _MY_ID)
        u = up.NodeUpgrader(env["settings"])
        env["db"].write_bytes(b"snapshot-bytes")
        u._make_snapshot()
        env["db"].write_bytes(b"current-bytes")
        state = {"phase": "creating", "target": "9", "image_ref": "r", "new_id": None, "token": "tok-1", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        assert await u.recover_on_boot() is True
        assert half.removed is True
        assert old.name == "llmpod-node"
        assert env["db"].read_bytes() == b"current-bytes"  # 快照未还原
        assert not u._snapshot_path().exists()
        assert not (env["data"] / up.STATE_FILE).exists()

    async def test_creating_phase_foreign_holder_keeps_state(self, env, monkeypatch):
        """creating 相位 + 占名者不带本次升级令牌（异源容器）→ 不敢动，保留状态
        文件下次启动再试。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        stranger = FakeContainer("llmpod-node", cid="stranger-id")
        stranger.attrs["Config"]["Env"] = [f"{up.DANCE_TOKEN_ENV}=other-token"]
        cli.add(stranger)
        _wire_self_id(monkeypatch, _MY_ID)
        u = up.NodeUpgrader(env["settings"])
        state = {"phase": "creating", "target": "9", "image_ref": "r", "new_id": None, "token": "tok-1", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        assert await u.recover_on_boot() is True
        assert stranger.removed is False
        assert cli.find("llmpod-node-old").name == "llmpod-node-old"  # 未 rename（名字被占）
        assert (env["data"] / up.STATE_FILE).exists()  # 保留待下次

    async def test_new_container_existed_restores_snapshot(self, env, monkeypatch):
        """新容器存在过的失败残留（created/started 相位）→ 快照还原（撤销新
        容器可能的写入）。"""
        cli = FakeDockerClient()
        old = cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        half = cli.add(FakeContainer("llmpod-node", cid="new-id"))
        _wire_self_id(monkeypatch, _MY_ID)
        u = up.NodeUpgrader(env["settings"])
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        monkeypatch.setattr(up, "READY_TIMEOUT_SECONDS", 0.05)
        env["db"].write_bytes(b"snapshot-bytes")
        u._make_snapshot()
        env["db"].write_bytes(b"written-by-new")
        state = {"phase": "started", "target": "9", "image_ref": "r", "new_id": half.id, "token": "tok-1", "ts": 1.0}
        (env["data"] / up.STATE_FILE).write_text(json.dumps(state))
        assert await u.recover_on_boot() is True
        assert env["db"].read_bytes() == b"snapshot-bytes"
        assert old.name == "llmpod-node"


# ---- 残活前任自检（名字被接管者持有 → rm -f 自己）----

_MY_ID = "a" * 64
_HOLDER_ID = "b" * 64


def _wire_self_id(monkeypatch, my_id: str | None) -> None:
    """把 _self_container_id 固定为指定 Id（boot 恢复/自检的身份判定都用它）。"""
    monkeypatch.setattr(up, "_self_container_id", lambda cli=None: my_id)


class TestRetireIfSuperseded:
    """背景：升级窗口整机重启，新容器先清状态文件、前任在 -old 名下复活成往复互踢。"""

    @staticmethod
    def _wire(monkeypatch, cli, my_id=_MY_ID):
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        monkeypatch.setattr(up, "_self_container_id", lambda cli=None: my_id)

    async def test_superseded_by_running_holder_self_removes(self, env, monkeypatch, no_exit):
        """名下 running 持有者 Id ≠ 本进程 → 按自身 Id rm -f 自己（不碰接管者）。"""
        cli = FakeDockerClient()
        me = cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        holder = cli.add(FakeContainer("llmpod-node", cid=_HOLDER_ID))  # 接管者
        self._wire(monkeypatch, cli)
        u = up.NodeUpgrader(env["settings"])
        with pytest.raises(SystemExit):
            await u.retire_if_superseded()
        assert me.removed is True
        assert holder.removed is False  # 只删自己，绝不按名字误杀接管者
        assert no_exit == [0]

    async def test_i_am_the_holder_noop(self, env, monkeypatch):
        """名下容器就是本进程（正常节点/接管者自检）→ 不动作。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node", cid=_MY_ID))
        self._wire(monkeypatch, cli)
        u = up.NodeUpgrader(env["settings"])
        await u.retire_if_superseded()  # 不抛 SystemExit 即通过

    async def test_holder_stopped_noop(self, env, monkeypatch):
        """持有者异 Id 但未运行（接管者 crash-loop）→ 不动作，交既有路径。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        cli.add(FakeContainer("llmpod-node", cid=_HOLDER_ID, status="exited"))
        self._wire(monkeypatch, cli)
        u = up.NodeUpgrader(env["settings"])
        await u.retire_if_superseded()

    async def test_name_free_noop(self, env, monkeypatch):
        """名字无人持有（正常首启/接管者尚未 create）→ 不动作。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node-old", cid=_MY_ID))
        self._wire(monkeypatch, cli)
        u = up.NodeUpgrader(env["settings"])
        await u.retire_if_superseded()

    async def test_busy_dance_skips_check(self, env, monkeypatch):
        """替换流程进行中不触发：生死由替换流程管理（等 ready 确认后退位，提前退位丢回滚）。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node", cid=_HOLDER_ID))
        self._wire(monkeypatch, cli)
        u = up.NodeUpgrader(env["settings"])
        u._busy = True
        await u.retire_if_superseded()

    async def test_self_id_unresolvable_fail_open(self, env, monkeypatch):
        """解析不出自身 Id（非 docker 运行时）→ fail-open 不拦截。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node", cid=_HOLDER_ID))
        self._wire(monkeypatch, cli, my_id=None)
        u = up.NodeUpgrader(env["settings"])
        await u.retire_if_superseded()

    async def test_docker_unavailable_fail_open(self, env, monkeypatch):
        """docker 异常 → fail-open（log.exception 后按正常逻辑继续）。"""

        def _boom():
            raise RuntimeError("docker down")

        monkeypatch.setattr(up, "_self_container_id", lambda: _MY_ID)
        monkeypatch.setattr(up, "get_docker_client", _boom)
        u = up.NodeUpgrader(env["settings"])
        await u.retire_if_superseded()

    def test_mountinfo_id_extract(self):
        """/etc/hostname 等 bind-mount 源路径含 64 hex 容器 Id（host 网络下唯一
        可用的自识别源：hostname=宿主名、cgroup v2=0::/）。"""
        # 真实容器里捕获的 mountinfo 形态（含 hosts/hostname/resolv.conf 三行）
        text = "\n".join(
            [
                "1598 1548 0:72 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/containerd/x",
                "1663 1598 252:0 /var/lib/docker/containers/"
                "f29c73b220b4eef4ae27ace32d59a31febb048183848a4a25f746c146456d492/resolv.conf"
                " /etc/resolv.conf rw,relatime - ext4 /dev/mapper/x rw",
                "1665 1598 252:0 /var/lib/docker/containers/"
                "f29c73b220b4eef4ae27ace32d59a31febb048183848a4a25f746c146456d492/hosts"
                " /etc/hosts rw,relatime - ext4 /dev/mapper/x rw",
            ]
        )
        assert (
            up._container_id_from_mountinfo(text) == "f29c73b220b4eef4ae27ace32d59a31febb048183848a4a25f746c146456d492"
        )
        # 非 docker 运行时 / 短 id 路径 → None
        assert up._container_id_from_mountinfo("1 2 0:3 / / proc rw - proc proc rw") is None
        assert up._container_id_from_mountinfo("") is None

    def test_mountinfo_custom_data_root(self):
        """data-root 挪盘（daemon.json 迁独立盘）：按实报前缀才能识别，默认
        /var/lib/docker 前缀下同一段 mountinfo 识别不出。"""
        cid = "a1b2c3d4" + "0" * 56
        line = f"1665 1598 252:0 /data/docker/containers/{cid}/hosts /etc/hosts rw,relatime - ext4 /dev/mapper/x rw"
        assert up._container_id_from_mountinfo(line, "/data/docker") == cid
        assert up._container_id_from_mountinfo(line) is None


# ---- 结果形状 ----


class TestOutcomeShape:
    def test_to_params(self):
        out = up.UpgradeOutcome("pull_failed", error_class="auth_failure", detail="x", target_version="9")
        p = out.to_params("n1")
        assert p == {
            "node_id": "n1",
            "result": "pull_failed",
            "target_version": "9",
            "error_class": "auth_failure",
            "detail": "x",
        }


# ---- 注销自清理（self_destruct：本进程只起旁路 + unlink 身份 + rm 自己，
# 引擎清扫/删卷在旁路容器脚本里做——脚本语义用 exec 真跑验证）----


class TestSelfDestruct:
    def _upgrader(self, env, id_file, node_id="") -> up.NodeUpgrader:
        """settings.node_id_file 指到 tmp 路径（默认相对 CWD，不随 fixture 走）。"""
        return up.NodeUpgrader(Settings(node_task_db=str(env["db"]), node_id_file=str(id_file), node_id=node_id))

    def _run_sidecar_script(self, cli, record: dict, monkeypatch) -> None:
        """把旁路容器收到的脚本在本地真跑一遍（docker.from_env 换成 fake）。

        前置：正主容器已被移除（模拟 rm 自己完成后的世界）——按脚本里锁定的
        容器 Id 清单标记，不按名字（窗口内重装的同名新节点不是正主）。
        """
        import docker as docker_pkg

        script = record["entrypoint"][2]
        assert record["entrypoint"][:2] == ["python3", "-c"]
        for c in list(cli.all_containers):
            if c.name.endswith("-volreaper") or f"'{c.id}'" in script:
                c.removed = True
        monkeypatch.setattr(docker_pkg, "from_env", lambda: cli, raising=True)
        exec(compile(script, "sidecar", "exec"), {})

    async def test_removes_container_and_drops_node_id(self, env, no_exit, monkeypatch):
        """删容器成功 → 数据卷里的 node_id 一并作废（注销标记期内同机重装不撞旧身份）。"""
        cli = FakeDockerClient()
        me = cli.add(FakeContainer("llmpod-node"))
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("decommission")
        assert me.removed is True
        assert not id_file.exists()

    async def test_container_already_gone_still_drops_node_id(self, env, no_exit, monkeypatch):
        """容器已不存在（NotFound）→ 旁路清理照起、身份照样作废。"""
        cli = FakeDockerClient()
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("register_nack: decommissioned")
        assert not id_file.exists()

    async def test_missing_name_keeps_node_id(self, env, no_exit, monkeypatch):
        """无容器名（存量节点）→ 不作废身份：容器没删成，清了会让 restart 复活成新节点。"""
        monkeypatch.delenv("LLMPOD_NODE_CONTAINER_NAME")
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("decommission")
        assert id_file.exists()

    async def test_docker_unreachable_keeps_identity(self, env, no_exit, monkeypatch):
        """docker 连不上 → 不作废身份（清了会让 restart 以新身份复活绕开注销标记）。"""
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")

        def boom():
            raise OSError("docker socket gone")

        monkeypatch.setattr(up, "get_docker_client", boom)
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("decommission")
        assert id_file.exists()

    async def test_remove_failure_restores_identity(self, env, no_exit, monkeypatch):
        """rm 自己失败 → 回写 node_id 保旧身份：restart 后命中注销标记重走自清理。"""
        cli = FakeDockerClient()
        me = cli.add(FakeContainer("llmpod-node"))
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")

        def _fail_remove(force=False):
            raise RuntimeError("daemon busy")

        monkeypatch.setattr(me, "remove", _fail_remove)
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("decommission")
        assert me.removed is False
        assert id_file.read_text() == "node-uuid-1"

    async def test_sidecar_entrypoint_sock_and_script(self, env, no_exit, monkeypatch):
        """旁路容器必须：覆盖 entrypoint（默认入口按角色 argparse 会 exit 2）、
        挂 docker.sock、禁网络、退出自删；脚本带容器名/-old/node_id/卷名。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        dest = str(env["data"].resolve())
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [{"Type": "volume", "Name": "llmpod-node-data", "Destination": dest}]
        cli.add(me)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        id_file = env["data"] / "node_id"
        id_file.write_text("nid-file-9")
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file, node_id="").self_destruct("decommission")
        assert me.removed is True
        # settings.node_id 为空（首进程形态）→ 身份取自文件（nid-file-9）
        records = [r for r in cli.created if r["name"] == "llmpod-node-volreaper"]
        assert len(records) == 1
        r = records[0]
        assert r["image"] == "reg/llmpod:9.9.9"
        assert r["entrypoint"][:2] == ["python3", "-c"]
        assert r["remove"] is True and r["network_disabled"] is True
        assert r["volumes"] == {"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}}
        script = r["entrypoint"][2]
        assert "'id-llmpod-node'" in script  # 目标在起旁路前解析为容器 Id 锁定
        assert "'llmpod-node'" not in script and "'llmpod-node-old'" not in script  # 绝不按名字删
        assert "nid-file-9" in script
        assert "'llmpod-node-data'" in script
        compile(script, "sidecar", "exec")

    async def test_sidecar_script_semantics(self, env, no_exit, monkeypatch):
        """真跑旁路脚本：等正主消失后删本节点引擎（label 圈定）+ 删卷；
        同机其他节点与无关容器不动。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        dest = str(env["data"].resolve())
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [{"Type": "volume", "Name": "llmpod-node-data", "Destination": dest}]
        cli.add(me)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        id_file = env["data"] / "node_id"

        def engine(name: str, node_id: str, status: str = "running") -> FakeContainer:
            c = FakeContainer(name, status=status)
            c.attrs["Config"]["Labels"] = {"llmpod.managed": "true", "llmpod.node-id": node_id}
            return cli.add(c)

        mine = engine("llmpod-d1-inst1", "nid-1")
        mine_stopped = engine("llmpod-d2-inst2", "nid-1", status="exited")
        other_node = engine("llmpod-d3-inst3", "nid-2")
        stranger = cli.add(FakeContainer("nginx-1"))
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file, node_id="nid-1").self_destruct("decommission")
        assert not mine.removed  # 本进程不动引擎（重活全在旁路）
        record = [r for r in cli.created if r["name"] == "llmpod-node-volreaper"][0]

        self._run_sidecar_script(cli, record, monkeypatch)
        assert mine.removed and mine_stopped.removed
        assert not other_node.removed and not stranger.removed
        assert cli.volumes_removed == ["llmpod-node-data"]

    async def test_bind_mount_data_dir_keeps_volume(self, env, no_exit, monkeypatch):
        """数据目录是 bind 宿主路径（挂进来的尽量不删）→ 脚本不带卷名、任务库保留。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [
            {"Type": "bind", "Source": "/mnt/big/llmpod-data", "Destination": str(env["data"].resolve())},
        ]
        cli.add(me)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("decommission")
        record = [r for r in cli.created if r["name"] == "llmpod-node-volreaper"][0]
        script = record["entrypoint"][2]
        assert "llmpod-node-data" not in script  # 不带具名卷
        self._run_sidecar_script(cli, record, monkeypatch)
        assert cli.volumes_removed == []
        assert env["db"].exists()  # bind 目录里的任务库保留
        assert not id_file.exists()  # 身份仍作废（本进程 unlink 兜底）

    async def test_ancestor_volume_mount_matched(self, env, no_exit, monkeypatch):
        """卷挂在数据目录的祖先路径（-v vol:/app，data 在 /app/data）→ 同样回收。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        me = FakeContainer("llmpod-node")
        parent = str(env["data"].resolve().parent)
        me.attrs["Mounts"] = [{"Type": "volume", "Name": "vol-app", "Destination": parent}]
        cli.add(me)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await self._upgrader(env, env["data"] / "node_id").self_destruct("decommission")
        record = [r for r in cli.created if r["name"] == "llmpod-node-volreaper"][0]
        assert "'vol-app'" in record["entrypoint"][2]

    async def test_sidecar_skipped_without_local_image(self, env, no_exit, monkeypatch):
        """本地无镜像（run 起不来）→ 不起旁路，仅收容器与身份，日志留手动清理线索。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node"))
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:missing")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        id_file = env["data"] / "node_id"
        id_file.write_text("node-uuid-1")
        with pytest.raises(SystemExit):
            await self._upgrader(env, id_file).self_destruct("decommission")
        assert not [r for r in cli.created if r.get("name", "").endswith("-volreaper")]
        assert not id_file.exists()

    async def test_reinstalled_same_name_node_survives(self, env, no_exit, monkeypatch):
        """等待窗口内同机重装（同名容器、全新 Id、复用默认数据卷）→ 旁路脚本
        绝不按名字删它，数据卷因仍被它挂载而跳过删除。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        dest = str(env["data"].resolve())
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [{"Type": "volume", "Name": "llmpod-node-data", "Destination": dest}]
        cli.add(me)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await self._upgrader(env, env["data"] / "node_id", node_id="nid-1").self_destruct("decommission")
        record = [r for r in cli.created if r["name"] == "llmpod-node-volreaper"][0]

        # 重装：正主已删、同名新容器（异 Id、全新身份）接管名字并挂同一数据卷
        reinstalled = FakeContainer("llmpod-node", cid="id-reinstalled-new")
        reinstalled.attrs["Mounts"] = [{"Type": "volume", "Name": "llmpod-node-data", "Destination": dest}]
        cli.add(reinstalled)
        self._run_sidecar_script(cli, record, monkeypatch)
        assert reinstalled.removed is False  # 不按名字删
        assert cli.volumes_removed == []  # 卷被新节点占用 → 跳过

    async def test_stopped_old_residue_removed_without_full_wait(self, env, no_exit, monkeypatch):
        """残留的已停 -old（Id 在起旁路时解析锁定）→ 等待循环不被它挡满 120s，
        按 Id 直接清掉；不殃及同名在跑的新节点。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        stale_old = FakeContainer("llmpod-node-old", status="exited")
        me = cli.add(FakeContainer("llmpod-node"))
        cli.add(stale_old)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        with pytest.raises(SystemExit):
            await self._upgrader(env, env["data"] / "node_id", node_id="nid-1").self_destruct("decommission")
        record = [r for r in cli.created if r["name"] == "llmpod-node-volreaper"][0]
        assert f"'{stale_old.id}'" in record["entrypoint"][2]  # Id 已锁定进脚本

        # 模拟 rm 自己完成后的世界：正主消失，-old 残留（已停）
        me.removed = True
        import docker as docker_pkg

        monkeypatch.setattr(docker_pkg, "from_env", lambda: cli, raising=True)
        exec(compile(record["entrypoint"][2], "sidecar", "exec"), {})
        assert stale_old.removed is True

    async def test_self_destruct_clears_upgrade_residue(self, env, no_exit, monkeypatch):
        """注销作废身份时连数据卷里的升级残留一起清——防同机重装复用数据卷时，
        前任的 boot 恢复把旧快照盖进新节点的任务库。"""
        cli = FakeDockerClient()
        cli.images_local.add("reg/llmpod:9.9.9")
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [
            {"Type": "bind", "Source": "/mnt/big/llmpod-data", "Destination": str(env["data"].resolve())}
        ]
        cli.add(me)
        monkeypatch.setenv(up._IMAGE_ENV_KEY, "reg/llmpod:9.9.9")
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)
        u = self._upgrader(env, env["data"] / "node_id")
        (env["data"] / up.STATE_FILE).write_text(json.dumps({"phase": "started"}))
        u._make_snapshot()
        with pytest.raises(SystemExit):
            await u.self_destruct("decommission")
        assert not (env["data"] / up.STATE_FILE).exists()
        assert not u._snapshot_path().exists()


# ---- _data_volume_name：数据卷定位（bind 形态不误删无关卷）----


class TestDataVolumeName:
    def _upgrader(self, env) -> up.NodeUpgrader:
        return up.NodeUpgrader(env["settings"])

    def test_root_mounted_volume_not_matched_for_bind_data_dir(self, env):
        """数据目录是 bind（无具名卷覆盖）时，挂在容器根 / 的具名卷绝不能被
        误当成数据卷（旧实现的空串匹配会中招，交给旁路容器整卷删掉）。"""
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [
            {"Type": "bind", "Source": "/mnt/big/llmpod-data", "Destination": str(env["data"].resolve())},
            {"Type": "volume", "Name": "unrelated-root-vol", "Destination": "/"},
        ]
        cli = FakeDockerClient()
        cli.add(me)
        assert self._upgrader(env)._data_volume_name(me) == ""

    def test_exact_volume_matched(self, env):
        me = FakeContainer("llmpod-node")
        me.attrs["Mounts"] = [{"Type": "volume", "Name": "llmpod-node-data", "Destination": str(env["data"].resolve())}]
        assert self._upgrader(env)._data_volume_name(me) == "llmpod-node-data"


class TestDanceCancelled:
    """取消路径：通道关闭取消升级协程时不得永久卡维护态。

    真实来源是通道关闭取消升级后台任务——测试里用独立 task + 线程内
    call_soon_threadsafe(cancel) 复现「await to_thread 时被取消」的时序。
    """

    async def test_cancel_after_rename_writes_state_and_resumes(self, env, agent, monkeypatch):
        """rename 后（create 前）被取消：落 cancelled 相位 + 退出维护 + 重抛 CancelledError。

        取消点选 me.reload（rename 后的 to_thread 目标，实例桩可持久打上）——
        覆盖「新容器尚未 create、老容器已改名」的窗口：boot 恢复按 cancelled 早
        相位 rename 回原名收敛。
        """
        cli = FakeDockerClient()
        me = cli.add(FakeContainer("llmpod-node"))
        cli.images_local.add("reg.local/a:1")
        u, calls = _make_upgrader(env, agent, cli, monkeypatch)

        dance_box: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()

        def _reload_then_cancel():
            loop.call_soon_threadsafe(dance_box[0].cancel)
            # 不 sleep：call_soon_threadsafe 在事件循环线程唤醒后派发，此刻升级协程
            # 正挂在 to_thread(reload) 的结果等待上，取消即达
            return None

        me.reload = _reload_then_cancel

        task = asyncio.ensure_future(u._dance("99.0.0", "reg.local/a:1"))
        dance_box.append(task)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        # 退出维护（resume 被调）+ 状态文件落 cancelled 相位（new_id 空 = 早相位）
        assert calls == ["suspend", "resume"]
        state = json.loads((env["data"] / up.STATE_FILE).read_text())
        assert state["phase"] == "cancelled:"
        assert me.name == "llmpod-node-old"  # rename 已生效，恢复留待 boot 收敛

    async def test_cancel_during_suspend_writes_state_and_resumes(self, env, agent, monkeypatch):
        """suspend 阶段被取消：同样写状态 + resume，不卡维护。"""
        cli = FakeDockerClient()
        cli.add(FakeContainer("llmpod-node"))
        calls, _suspend, resume = agent

        dance_box: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()

        async def _suspend_then_cancel():
            calls.append("suspend")
            loop.call_soon_threadsafe(dance_box[0].cancel)

        u = up.NodeUpgrader(env["settings"], suspend=_suspend_then_cancel, resume=resume)
        monkeypatch.setattr(up, "get_docker_client", lambda: cli)

        task = asyncio.ensure_future(u._dance("99.0.0", "reg.local/a:1"))
        dance_box.append(task)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert calls == ["suspend", "resume"]
        state = json.loads((env["data"] / up.STATE_FILE).read_text())
        assert state["phase"] == "cancelled:"
