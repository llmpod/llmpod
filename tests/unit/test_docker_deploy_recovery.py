"""慢创建、Docker 失联和取消的边界；不连接真实 Docker daemon。"""

import asyncio
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock

import pytest
from docker.errors import APIError, NotFound
from requests import Response
from requests.exceptions import ConnectionError, ConnectTimeout, ReadTimeout

from llmpod.apps.node.deployers import docker as dmod
from llmpod.apps.node.exceptions import TaskCancelled, TaskExecutionError
from llmpod.tunnel.schemas import DeploySpec


SPEC = DeploySpec(image="vllm:test", name="llmpod-recovery-test", labels={"llmpod.instance-id": "i1"})


def container(*, running=False):
    return Mock(
        attrs={
            "Config": {"Image": SPEC.image, "Labels": dict(SPEC.labels)},
            "State": {"Running": running, "Status": "running" if running else "created"},
        }
    )


def api_error(status):
    response = Response()
    response.status_code = status
    response.url = "http://docker/containers/create"
    return APIError("Docker API error", response=response, explanation="test daemon error")


@pytest.fixture
def client(monkeypatch):
    c = Mock()
    monkeypatch.setattr(dmod, "_get_client", lambda: c)
    return c


@pytest.fixture
def clock(monkeypatch):
    times = [0.0]

    def sleep(seconds):
        times[0] += seconds

    monkeypatch.setattr(dmod, "time", SimpleNamespace(monotonic=lambda: times[0], sleep=sleep))
    monkeypatch.setattr(dmod, "_CREATE_CONFIRM_TIMEOUT", 6.0)
    return times


async def test_read_timeout_waits_for_created_container_without_recreating(client, clock):
    c = container()
    client.containers.create.side_effect = ReadTimeout("create response timed out")
    client.containers.get.side_effect = [NotFound("pending"), NotFound("pending"), c]

    result = await dmod.DockerDeployer().deploy(SPEC)

    assert result.success
    assert clock[0] == 4
    client.containers.create.assert_called_once()
    c.start.assert_called_once()
    c.remove.assert_not_called()


async def test_recovered_running_container_is_not_restarted(client, clock):
    c = container(running=True)
    client.containers.create.side_effect = ReadTimeout("create response timed out")
    client.containers.get.return_value = c
    assert (await dmod.DockerDeployer().deploy(SPEC)).success
    c.start.assert_not_called()
    c.remove.assert_not_called()


@pytest.mark.parametrize("error", [ConnectionError("socket closed"), ConnectTimeout("connect timeout")])
async def test_connect_failure_does_not_enter_creation_wait(client, clock, error):
    client.containers.create.side_effect = error
    with pytest.raises(TaskExecutionError, match="Docker 连接失败"):
        await dmod.DockerDeployer().deploy(SPEC)
    client.containers.get.assert_not_called()
    assert clock[0] == 0


@pytest.mark.parametrize(
    "error, message",
    [
        (ConnectionError("socket closed"), "Docker 连接失败"),
        (ReadTimeout("inspect timeout"), "Docker 状态查询超时"),
        (api_error(500), "Docker 状态查询失败"),
    ],
)
async def test_confirmation_fault_fails_without_more_creates(client, clock, error, message):
    client.containers.create.side_effect = ReadTimeout("create response timed out")
    client.containers.get.side_effect = error
    with pytest.raises(TaskExecutionError, match=message):
        await dmod.DockerDeployer().deploy(SPEC)
    client.containers.create.assert_called_once()
    client.containers.get.assert_called_once()
    assert clock[0] == 0


async def test_reachable_daemon_with_no_result_has_a_deadline(client, clock):
    client.containers.create.side_effect = ReadTimeout("create response timed out")
    client.containers.get.side_effect = NotFound("not registered")
    with pytest.raises(TaskExecutionError, match="Docker 可达.*确认期限"):
        await dmod.DockerDeployer().deploy(SPEC)
    assert clock[0] == 6
    client.containers.create.assert_called_once()


async def test_reserved_name_not_yet_inspectable_waits_instead_of_replacing(client, clock):
    c = container()
    client.containers.create.side_effect = api_error(409)
    client.containers.get.side_effect = [NotFound("reserved"), NotFound("reserved"), c]
    assert (await dmod.DockerDeployer().deploy(SPEC)).success
    client.containers.create.assert_called_once()
    c.start.assert_called_once()
    c.remove.assert_not_called()


async def test_existing_container_replacement_is_preserved(client):
    old, new = container(), container()
    client.containers.create.side_effect = [api_error(409), new]
    client.containers.get.return_value = old
    assert (await dmod.DockerDeployer().deploy(SPEC)).success
    old.stop.assert_called_once_with(timeout=10)
    old.remove.assert_called_once_with(force=True)
    new.start.assert_called_once()
    assert client.containers.create.call_count == 2


@pytest.mark.parametrize("mismatch", ["image", "instance"])
async def test_recovery_does_not_adopt_or_delete_another_container(client, clock, mismatch):
    c = container()
    if mismatch == "image":
        c.attrs["Config"]["Image"] = "unrelated:image"
    else:
        c.attrs["Config"]["Labels"]["llmpod.instance-id"] = "another-instance"
    client.containers.create.side_effect = ReadTimeout("create response timed out")
    client.containers.get.return_value = c
    with pytest.raises(TaskExecutionError, match="与本次部署不匹配"):
        await dmod.DockerDeployer().deploy(SPEC)
    c.start.assert_not_called()
    c.remove.assert_not_called()


async def test_start_timeout_waits_for_running_without_starting_twice(client, clock):
    c, running = container(), container(running=True)
    client.containers.create.return_value = c
    c.start.side_effect = ReadTimeout("start response timed out")
    client.containers.get.side_effect = [c, running]
    assert (await dmod.DockerDeployer().deploy(SPEC)).success
    c.start.assert_called_once()
    running.start.assert_not_called()
    assert clock[0] == 2


async def test_cancel_waits_for_late_creation_then_removes_without_starting(client, clock):
    cancel = asyncio.Event()
    c = container()
    client.containers.create.side_effect = ReadTimeout("create response timed out")

    def late(name):
        if not cancel.is_set():
            cancel.set()
            raise NotFound("still creating")
        return c

    client.containers.get.side_effect = late
    with pytest.raises(TaskCancelled):
        await dmod.DockerDeployer().deploy(SPEC, cancel_event=cancel)
    c.start.assert_not_called()
    c.remove.assert_called_once_with(force=True)


async def test_cancel_cleanup_connection_failure_is_an_execution_error(client, clock):
    cancel = asyncio.Event()
    c = container()

    def create(**kwargs):
        cancel.set()
        return c

    client.containers.create.side_effect = create
    c.remove.side_effect = ConnectionError("daemon disconnected")
    with pytest.raises(TaskExecutionError, match="Docker 连接失败"):
        await dmod.DockerDeployer().deploy(SPEC, cancel_event=cancel)
    c.start.assert_not_called()


async def test_task_cancellation_keeps_stop_behind_inflight_creation(client):
    entered, finish = Event(), Event()
    c = container()

    def create(**kwargs):
        entered.set()
        assert finish.wait(2), "test did not release create"
        return c

    client.containers.create.side_effect = create
    client.containers.get.side_effect = NotFound("cancel cleanup removed the container")
    deployer = dmod.DockerDeployer()
    task = asyncio.create_task(deployer.deploy(SPEC))
    stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        stop = asyncio.create_task(deployer.stop(SPEC.name))
        await asyncio.sleep(0)
        client.containers.get.assert_not_called()
        assert not task.done() and not stop.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if stop is not None:
            assert (await stop).success
    c.start.assert_not_called()
    c.remove.assert_called_once_with(force=True)
    assert SPEC.name not in dmod._deploy_locks


async def test_recovered_crashed_container_is_left_for_readiness_diagnostics(client, clock):
    c = container()
    c.attrs["State"].update(Status="exited", ExitCode=1)
    client.containers.create.side_effect = ReadTimeout("create response timed out")
    client.containers.get.return_value = c
    assert (await dmod.DockerDeployer().deploy(SPEC)).success
    c.start.assert_not_called()
    c.remove.assert_not_called()


async def test_container_removed_during_start_is_not_treated_as_pending_creation(client, clock):
    c = container()
    client.containers.create.return_value = c
    c.start.side_effect = ReadTimeout("start response timed out")
    client.containers.get.side_effect = NotFound("removed")
    with pytest.raises(TaskExecutionError, match="确认期间已不存在"):
        await dmod.DockerDeployer().deploy(SPEC)
    assert clock[0] == 0
