"""Node 自升级执行器：rename 替换流程（新旧容器替换式升级）。

收到 manager ``$node_upgrade`` 通知后执行（强制语义：不等在跑任务，取消标记中断，
由新容器启动恢复 + manager 对账重派续跑）。替换流程全程老容器进程存活、WS 不断——
manager 视角节点不 offline，引擎（兄弟容器）零影响。

状态机（相位落盘数据卷 upgrade_state.json，boot 恢复收敛用；**相位先落盘、后做
对应 docker 动作**——docker 动作已生效而磁盘无记录的窗口被打断后无法收敛）::

    0 预检+锁   进程内 _busy 标志（检查与置位间无 await 即原子；文件锁在 rm -f self
                后 finally 不执行会永久残留，故不落盘）。target ≤ 自身版本 → 忽略
                （只向前）；缺容器名/本地 tag → 上报 not_supported；残留 {name}-old
                的 running 持有者是异容器 → 上报 not_supported 交人工，是本进程
                （上次中断后自己活在 -old 名下）→ 上报指引 restart 由启动恢复收敛
    1 维护      拒新任务 + 取消在跑任务 + sqlite 快照 + 关连接（新容器独占数据卷）
    2 pull      镜像不在本地才拉；失败按 auth/not_found/network/unknown 分类上报，容器不动
    3 rename    renamed 相位先落盘，再把自己 rename 到 {name}-old
    4 create    creating 相位先落盘 → 原名 + 新镜像 + 整块 HostConfig 回放（低层
                API）+ 升级 env 覆写 + 升级令牌 env；create 成功才捕获 new_id、
                落 created 相位（此后一切新容器操作只认 id、禁止按名字反查
                ——相位未知时名字下是老容器自己，反查回滚会自杀）
    5 start     新容器，落 started 相位
    6 等就绪    轮询共享数据卷 ready marker（新容器 $register_ack 后写
                {version, ts, dance}；running ≠ 可用）。marker 只认**升级令牌
                匹配**的写出者——老容器重连也会写 marker（旧版本+新鲜时间戳），
                不带本次升级令牌的一律忽略继续等；同时监控 exited/restarting——
                crash 在回放的 restart policy 下是 backoff 循环，永不落 exited，
                只看 exited 抓不住
    7a 成功     清状态/快照 → 上报 → docker rm -f 自己（rm 失败也硬退出：落兜底
                会让老容器退出维护与新容器双活；restart 拉起后撞 retire_if_superseded
                收敛；进程 exit 137；restart 策略随容器消失失效不复活）
    7b 失败     按 new_id 删新容器 + 恢复 sqlite 快照 + rename 回原名 + 重开
                sqlite + 退出维护 + 上报（附日志尾）。回滚任一步未完成 → 保留
                状态文件与快照、**留在维护态**（此刻退出维护继续写库，会让下次
                启动的快照还原丢掉这段写入），上报指引 docker restart 收敛
    兜底        未预期异常什么都不动（相位未知时任何 docker 操作都可能误伤），
                落盘相位 + 上报 unknown_error，交 boot 恢复收敛

boot 恢复（``recover_on_boot``）：启动发现 upgrade_state.json → 按相位收敛。新旧容器
的身份用升级令牌判定（env ``LLMPOD_UPGRADE_DANCE_TOKEN``，名称保留；create 时注入，同时落盘
state——host 网络下 hostname 是宿主名，不能当容器 id 用）。creating 相位（create 进行
中被打断）下 {name} 可能已被无主半成品占住：按令牌 env 识别，带令牌的可清，不带的
是异源容器交人工。快照还原只发生在**新容器存在过**的残留上（它启动可能写过库）；
从未建出新容器的失败库没人动过，还原反而拿替换流程开始时的旧快照盖掉此后正常运行写入
的数据。升级中断（断电/进程被杀）后重启不可能留下无法恢复的同名冲突。

挂载回放读 inspect 顶层 ``attrs["Mounts"]`` 做断言——``HostConfig.Mounts`` 只存
``--mount`` 形式，``-v`` 形式不进该字段，按它回放会造出零挂载的静默残废容器
（无 sock/数据卷）。实际绑定走 ``HostConfig.Binds``（整块 HostConfig 回放自带）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

import llmpod
from llmpod.apps.node.deployers.docker import get_docker_client
from llmpod.config.settings import Settings
from llmpod.services.version_service import parse_version
from llmpod.utils import now_utc
from llmpod.utils.docker_naming import LABEL_MANAGED, LABEL_NODE_ID
from docker.errors import NotFound

log = logging.getLogger("llmpod.apps.node.upgrader")

# 升级状态/就绪标记文件名（都在数据卷 = node_task_db 的父目录，新旧容器共享）
STATE_FILE = "upgrade_state.json"
READY_FILE = "ready_marker.json"

# 一次性旁路清理容器的内嵌脚本（注销自清理的重活都在这里做——本容器 rm 自己后
# 进程随即被杀，死后的语句不可靠，且重活放旁路能让 WS 尽快断开，manager 注销
# 等待窗只有 10s）。%(...)s 值全部经 repr() 注入。执行序：
# 1) 等正主消失：目标容器 Id 清单里还有 running 的就等（覆盖升级中途被注销的
#    窗口——异 Id 接管者随后也会命中注销标记走自清理）；
# 2) 清残留：按 Id 强删清单里的容器；
# 3) 删本节点推理容器（label node-id 圈定；同机共存的其他节点不受影响）；
# 4) 删具名数据卷（bind 挂载形态 VOLUME 为空串，不删）。
# 铁律：**只认容器 Id，绝不按名字**（与 retire_if_superseded 同源）——等待窗口
# 内同机重装的新节点会顶走 {name}，按名删会误杀它、连带删掉它正挂载的数据卷。
# Id 清单在起旁路前一刻（本进程还活着、名字归属明确时）由 Python 侧一次解析锁定。
# 数据卷删除前先查占用：仍被任何容器挂载（含同名重装接手的新节点）就跳过，
# docker 对在用卷本会拒绝，显式查一遍是让「接手保护」不依赖 daemon 报错路径。
_CLEANUP_SCRIPT = """
import time
import docker
from docker.errors import NotFound

IDS = %(ids)s
NODE_ID, LABEL_MANAGED, LABEL_NODE_ID, VOLUME = %(node_id)s, %(managed)s, %(node_label)s, %(volume)s

cli = docker.from_env()
deadline = time.time() + 120


def running(ref):
    # 已停容器不挡道（无需等它死）；docker 查询异常保守当作活着——daemon 短暂
    # 不可用时宁可等满窗口，也不能误判消失就动手删
    if not ref:
        return False
    try:
        return cli.containers.get(ref).status == "running"
    except NotFound:
        return False
    except Exception:
        return True


while time.time() < deadline:
    if not any(running(i) for i in IDS):
        break
    time.sleep(1)

for ref in IDS:
    try:
        cli.containers.get(ref).remove(force=True)
    except Exception:
        pass

if NODE_ID:
    labels = [LABEL_MANAGED + "=true", LABEL_NODE_ID + "=" + NODE_ID]
    for c in cli.containers.list(all=True, filters={"label": labels}):
        try:
            c.remove(force=True)
        except Exception:
            pass

if VOLUME:
    for _ in range(3):
        try:
            if cli.containers.list(all=True, filters={"volume": VOLUME}):
                break  # 有容器（如重装接手的新节点）在用，绝不删
            cli.volumes.get(VOLUME).remove()
            break
        except Exception:
            pass  # 刚删完推理容器 daemon 可能尚未释放占用，稍候重试
        time.sleep(5)
""".strip()

# 升级令牌 env（create 时注入新容器 + 落盘 state；boot 恢复据此判定「我是新容器」）
DANCE_TOKEN_ENV = "LLMPOD_UPGRADE_DANCE_TOKEN"

# ready marker 等待上限（秒）：新容器含 python 启动 + WS 连接 + 注册（manager 重启窗口
# 也在内），5 分钟是「宁可多等也不误回滚」的宽值
READY_TIMEOUT_SECONDS = 300
READY_POLL_INTERVAL = 2
# rm -f self 前给结果上报留的刷出时间（尽力而为，多等无益）
_REPORT_FLUSH_SECONDS = 2

# 相位词表（落盘值，boot 恢复按此分支；已发布值勿改）。creating = create 调用
# 进行中：{name} 可能已被不带 new_id 的半成品占住，boot 恢复按令牌识别处置。
# cancelled:<new_id> = 升级协程被取消（通道关闭/manager 踢旧连接）：new_id 空 =
# rename 前取消（回家即可），非空 = 新容器已 create/start，恢复按 created/started
# 同款「等就绪或清半成品」收敛
PHASE_RENAMED = "renamed"
PHASE_CREATING = "creating"
PHASE_CREATED = "created"
PHASE_STARTED = "started"
PHASE_CANCELLED = "cancelled"

# 整块 Env 回放后按新值覆写的升级镜像 env（否则老容器的旧镜像 ref 被带给新容器）
_IMAGE_ENV_KEY = "LLMPOD_NODE_IMAGE"

# daemon data-root 查询缓存（None=未查过）。data-root 由 daemon.json 决定、运行期
# 不变，本进程查一次够用；info() 是全量状态调用，不该在每次自识别时都打
_DATA_ROOT: str | None = None


def classify_pull_error(exc: Exception) -> str:
    """pull 异常 → 错误分类（auth_failure / image_not_found / network_error / unknown）。

    兼容不同 registry/mirror 的措辞：Docker Hub 直连对不存在镜像报
    ``manifest ... not found`` / ``manifest unknown``；部分镜像加速器
    报 ``failed to resolve reference``（HEAD 请求异常 = 引用不存在），也归
    image_not_found。
    """
    msg = str(exc).lower()
    if "pull access denied" in msg or "unauthorized" in msg or "authentication" in msg or "401" in msg:
        return "auth_failure"
    if (
        "not found" in msg
        or "404" in msg
        or "does not exist" in msg
        or "manifest unknown" in msg
        or "resolve reference" in msg
        or "unknown: failed to resolve" in msg
    ):
        return "image_not_found"
    if "timeout" in msg or "connection" in msg or "tls" in msg or "dns" in msg or "network" in msg:
        return "network_error"
    return "unknown"


def is_registry_ref(ref: str) -> bool:
    """是否可从 registry 拉取（自动升级的前提）。

    docker 语义：带 ``/`` 的名字就是 registry 命名空间（btpanel/llmpod:1 隐式
    docker.io；registry.local/ns/app:1 显式 host），daemon 会正常解析 pull。
    不带 ``/`` 的裸名（llmpod:latest）在本产品的部署约定里是本机 build/import 的
    本地 tag，跨节点无从 pull——自动升级对它无意义，交人工 save/load。
    """
    return "/" in ref


def _container_id_from_mountinfo(text: str, docker_root: str = "/var/lib/docker") -> str | None:
    """从 mountinfo 文本提取 docker 容器 Id；读不出返回 None。

    首选 ``/etc/hostname`` 的 bind-mount 源路径（docker 为每个容器挂
    ``<data-root>/containers/<id>/hostname``，host 网络也成立——此时
    hostname 是宿主名、cgroup 是 ``0::/``，常规手法全失效）；任意容器的
    hosts/resolv.conf 源路径同构，扫全表取第一个命中。``docker_root`` 传
    daemon 实报 data-root（挪盘机器上 /var/lib/docker 前缀恒不匹配）。
    """
    prefix = f"{docker_root.rstrip('/')}/containers/"
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        source = fields[3]
        if source.startswith(prefix):
            cid = source[len(prefix) :].split("/")[0]
            if len(cid) == 64:
                return cid
    return None


def _docker_data_root(cli) -> str:
    """daemon 实报的数据根目录（daemon.json data-root 可把容器目录挪到独立盘）。

    取 ``client.info()["DockerRootDir"]``，结果进程内缓存。拿不到 docker 连接时
    由调用方兜底，此处不吞异常。"""
    global _DATA_ROOT
    if _DATA_ROOT is None:
        _DATA_ROOT = str((cli.info() or {}).get("DockerRootDir") or "").strip()
    return _DATA_ROOT


def _self_container_id(cli) -> str | None:
    """本进程所在容器的 Id（64 hex）；读不出返回 None（非 docker 运行时 fail-open）。

    前缀优先用 daemon 实报 data-root（挪盘机器上默认前缀恒不匹配），识别不出再
    回落默认 /var/lib/docker（标准安装就是它）。"""
    try:
        text = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return None
    try:
        root = _docker_data_root(cli).rstrip("/")
    except Exception:
        root = ""  # daemon 暂不可达：只剩默认前缀，标准安装不受影响
    default_root = "/var/lib/docker"
    if root and root != default_root:
        cid = _container_id_from_mountinfo(text, root)
        if cid is not None:
            return cid
    return _container_id_from_mountinfo(text, default_root)


def _container_env_token(container) -> str:
    """读容器 inspect Config.Env 里的升级令牌；无令牌/读不出返回空串。"""
    try:
        env = (container.attrs.get("Config") or {}).get("Env") or []
    except Exception:
        return ""
    for entry in env:
        if entry.startswith(f"{DANCE_TOKEN_ENV}="):
            return entry.split("=", 1)[1]
    return ""


def _version_at_most(a: str, b: str) -> bool:
    """按日历版本号语义判断 ``a <= b``；任一解析不成形返回 False（不拦截，让替换流程自身兜底）。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return False
    return pa <= pb


async def _get_or_none(cli, ref: str):
    """containers.get，容器不存在返回 None（升级路径的「找不到就算了」语义）。"""
    try:
        return await asyncio.to_thread(cli.containers.get, ref)
    except NotFound:
        return None


class UpgradeOutcome:
    """升级结果（$node_upgrade_result 的 payload 载体）。"""

    def __init__(
        self,
        result: str,
        *,
        error_class: str | None = None,
        detail: str | None = None,
        target_version: str = "",
    ):
        self.result = result  # success|pull_failed|create_failed|start_failed|not_ready|not_supported|unknown_error
        self.error_class = error_class
        self.detail = detail
        self.target_version = target_version

    def to_params(self, node_id: str = "") -> dict:
        out: dict = {"node_id": node_id, "result": self.result, "target_version": self.target_version}
        if self.error_class:
            out["error_class"] = self.error_class
        if self.detail:
            out["detail"] = self.detail
        return out


class NodeUpgrader:
    """节点自升级执行器（挂 TunnelClient 的 $node_upgrade / $node_decommission 通知）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        suspend=None,
        resume=None,
        report_result=None,
    ):
        """:param suspend/resume: 进出维护模式回调（取消在跑任务+关 sqlite / 重开
        sqlite+恢复接收任务），由 apps/node 装配层注入。
        :param report_result: 结果上报回调 ``async def (UpgradeOutcome) -> None``。
        失败路径老容器活着 WS 必在；成功路径以新容器版本追平为准（通知丢了不影响语义）。
        """
        self._settings = settings
        self._suspend = suspend
        self._resume = resume
        self._report_result = report_result
        # 升级进行中标志（含排队中的提议）。事件循环单线程下「if 检查 → 赋值」
        # 之间无 await 即原子——并发提议只有第一个能置位，后续直接忽略，
        # 不需要再叠一把 asyncio.Lock。
        self._busy = False

    # ---- 对外入口 ----

    @property
    def busy(self) -> bool:
        """升级进行中（含维护窗口；TaskService 据此拒绝新任务）。"""
        return self._busy

    async def handle_upgrade(self, params: dict) -> None:
        """$node_upgrade 通知入口：{target_version, image_ref}。

        通知是「提议」：版本已达标/升级中 → 忽略，manager 无状态机（重连 hello 兜底）。
        """
        target = str(params.get("target_version") or "")
        image_ref = str(params.get("image_ref") or "")
        if not target or not image_ref:
            log.warning("node_upgrade: 缺 target_version/image_ref，忽略: %s", params)
            return
        if self._busy:
            log.info("node_upgrade: 升级已在进行，忽略重复提议")
            return
        self._busy = True  # 与上面的 if 之间无 await，原子：并发提议只有第一个能置位
        try:
            outcome = await self._dance(target, image_ref)
        finally:
            self._busy = False
        if outcome is not None:
            await self._report(outcome)

    async def self_destruct(self, detail: str = "") -> None:
        """注销自清理：起旁路清理容器 → 清本地升级残留 → unlink 身份 → rm -f 自己。

        清单与分工（重活全部在旁路容器里做——本容器 rm 自己后进程随即被杀
        exit 137，写在 remove 之后的语句等于没写；重活放旁路还能让 WS 尽快
        断开，manager 注销等待窗只有 10s）：
        - 旁路容器（{name}-volreaper）：按 Id 清单等本体系容器消失 → 删本节点
          推理容器（label node-id 圈定，含已停/残跑/崩溃取证的——节点已退出
          平台，取证价值终止）→ 删具名数据卷（整卷，含内容；仍被占用则跳过）；
        - 本进程：定位自身（容器 Id 优先，名字兜底）→ 起旁路 → 清数据卷里的
          升级残留（状态文件/快照——同机重装复用数据卷时绝不能让上一任的
          boot 恢复把旧快照盖进新节点的任务库）→ unlink node_id → rm -f 自己。
          rm 失败则回写 node_id 保持旧身份——restart 后命中注销标记重走本流程收敛，
          不复活成新节点；docker 不可达同理不清身份直接退。
        挂载进来的宿主路径（模型目录、extra_mounts、bind 形态的数据目录）不是
        我们创建的目录，旁路容器不碰，bind 形态只由本进程 unlink node_id 兜底。
        """
        name = os.environ.get("LLMPOD_NODE_CONTAINER_NAME", "")
        if not name:
            # 存量节点（引导脚本未注入容器名）：无法自定位，退出进程打醒目日志；
            # restart 策略 ~35s 重试一次再被 manager 拦下（有界噪声，提示手动 rm）。
            # 此分支不做任何回收——容器没删成，清了 node_id 会让 restart 以全新
            # 身份复活成新节点，违背注销意图。
            log.error("无法自清理：缺 LLMPOD_NODE_CONTAINER_NAME，请手动 docker rm 本容器。%s", detail)
            os._exit(1)
        try:
            cli = get_docker_client()
        except Exception:
            # 容器还活着且还要 restart：作废身份会让它以新身份复活（新身份绕开
            # 注销标记）。保持旧身份，重连被注销标记拦下后会再走 self_destruct 重试。
            log.exception("无法连接 docker，本次不自清理（保持旧身份待重试）：%s", detail)
            os._exit(1)
        node_id = self._current_node_id()
        identity_backup = None
        try:
            my_id = await asyncio.to_thread(_self_container_id, cli)
            me = await asyncio.to_thread(cli.containers.get, my_id or name)
            volume = self._data_volume_name(me)
            await self._spawn_cleanup_sidecar(cli, name, node_id, volume, own_id=str(me.id))
            log.warning("自清理：rm -f %s（%s）", name, detail or "decommission")
            identity_backup = self._read_identity()
            self._purge_upgrade_residue()
            self._drop_identity()
            await asyncio.to_thread(me.remove, force=True)
        except NotFound:
            # 容器已不在（外部删过/上一轮自清理删了容器但进程没死成）：推理容器
            # 仍要收——旁路容器照起（label 圈定不依赖容器在否）；数据卷名无从
            # 解析（挂载关系随容器 inspect 消失），留待人工；升级残留与身份照常作废。
            log.warning("自清理：容器 %s 已不存在（可能已被删），旁路清理照常执行（数据卷名未知，留待人工）", name)
            await self._spawn_cleanup_sidecar(cli, name, node_id, "", own_id=my_id or "")
            self._purge_upgrade_residue()
            self._drop_identity()
        except Exception:
            log.exception("自清理失败：容器 %s 需手动清理", name)
            if identity_backup is not None:
                self._restore_identity(identity_backup)
        os._exit(1)

    def _current_node_id(self) -> str:
        """自身节点身份。settings 启动期读的值在首进程内可能为空（$register_ack
        只落文件不回写 settings——bootstrap 后没重启过的节点是最常见形态），
        以文件为准兜底。"""
        if self._settings.node_id:
            return self._settings.node_id
        try:
            return Path(self._settings.node_id_file).read_text().strip()
        except OSError:
            return ""

    def _drop_identity(self) -> None:
        """作废数据卷里持久化的 node_id，下次引导以全新身份注册。"""
        path = Path(self._settings.node_id_file)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("注销作废 node_id 失败：%s（请手动删除后重新引导）", path)

    def _read_identity(self) -> bytes | None:
        """读出身份文件内容（回写用）；无内容/不可读返回 None。"""
        try:
            return Path(self._settings.node_id_file).read_bytes() or None
        except OSError:
            return None

    def _restore_identity(self, data: bytes) -> None:
        """自清理失败时回写身份：容器活着会 restart，保持旧身份走注销标记重试链。"""
        try:
            Path(self._settings.node_id_file).write_bytes(data)
        except OSError:
            log.warning("回写 node_id 失败：restart 后可能以新身份复活成新节点，请人工核对")

    def _purge_upgrade_residue(self) -> None:
        """清数据卷里的升级残留（状态文件 + sqlite 快照）。

        注销作废的是整套身份：残留的 upgrade_state.json 会让同机重装（复用同一
        数据卷）的新容器在 recover_on_boot 里按前任的相位收敛——守卫令牌全不
        匹配时直接 _restore_snapshot，把前任的旧任务库盖进新节点。先清掉再退场。
        """
        for cleaner in (self._clear_state, self._discard_snapshot):
            try:
                cleaner()
            except OSError:
                log.warning("注销清升级残留失败：%s（复用数据卷重装时请手动删除）", cleaner.__name__)

    def _data_volume_name(self, me) -> str:
        """数据目录所在挂载是具名卷 → 卷名（整卷回收）；bind 宿主路径 → 空串。

        匹配含祖先挂载（-v vol:/app 而数据目录是 /app/data 时同样落在卷内），
        嵌套挂载取最长匹配（最内层才是数据目录的直接卷）。best 为空串说明没有
        任何具名卷覆盖数据目录（纯 bind 形态）——直接返回，不做第二轮匹配：
        空串会误匹配「挂在容器根 / 的卷」，删到与节点无关的卷。
        """
        target = str(self._data_dir().resolve()).rstrip("/")
        best_dest = ""
        best_name = ""
        for m in me.attrs.get("Mounts") or []:
            if m.get("Type") != "volume":
                continue
            dest = str(m.get("Destination", "")).rstrip("/")
            if dest and (dest == target or target.startswith(dest + "/")) and len(dest) > len(best_dest):
                best_dest = dest
                best_name = str(m.get("Name") or "")
        return best_name

    async def _spawn_cleanup_sidecar(self, cli, name: str, node_id: str, volume: str, own_id: str = "") -> None:
        """起一次性旁路清理容器（脚本见 _CLEANUP_SCRIPT，分工见 self_destruct）。

        目标 Id 清单在**此刻**一次解析锁定（本进程还活着、名字归属明确）——旁路容器
        绝不按名字删容器：等待窗口内同机重装的新节点会顶走 {name}，按名删会误杀
        它并连带删掉它正挂载的数据卷。清单 = 自身 Id + 此刻名字 {name}/{name}-old
        归属者的 Id（覆盖升级中途被注销、名字在 -old 或接管者手里的窗口）。
        要点：镜像用自身（本地必有，run 不自动 pull，起前显式核验）；entrypoint
        必须显式覆盖——镜像默认入口按角色 argparse，直接给 command 会被当成
        角色名解析失败（实测 invalid choice exit 2，容器 --rm 即蒸发无痕）；
        挂 docker.sock、禁网络（只走 sock）；remove=True 退出自删。名字固定
        {name}-volreaper：上次残留未清时 run 名字冲突报错提示手动清，不静默叠加。
        """
        image = os.environ.get(_IMAGE_ENV_KEY, "")
        if not image:
            log.warning("注销清理：缺 %s 无法起旁路容器，推理容器/数据卷请手动清理", _IMAGE_ENV_KEY)
            return
        try:
            await asyncio.to_thread(cli.images.get, image)
        except Exception:
            log.warning("注销清理：本地无镜像 %s，旁路容器起不来，请手动清理", image)
            return
        ids: list[str] = []
        for ref in (own_id, name, f"{name}-old"):
            if not ref:
                continue
            try:
                holder = await asyncio.to_thread(cli.containers.get, ref)
            except Exception:
                continue  # 该名字此刻无人认领（或 docker 查不到），交 label 清扫兜底
            if str(holder.id) not in ids:
                ids.append(str(holder.id))
        script = _CLEANUP_SCRIPT % {
            "ids": repr(ids),
            "node_id": repr(node_id),
            "managed": repr(LABEL_MANAGED),
            "node_label": repr(LABEL_NODE_ID),
            "volume": repr(volume),
        }
        try:
            await asyncio.to_thread(
                cli.containers.run,
                image,
                entrypoint=["python3", "-c", script],
                name=f"{name}-volreaper",
                detach=True,
                remove=True,
                network_disabled=True,
                volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
            )
            log.warning(
                "注销旁路清理容器已起（%s-volreaper）：本容器消失后删推理容器%s",
                name,
                f"与数据卷 {volume}" if volume else "（bind 数据目录，不删卷）",
            )
        except Exception as e:
            log.warning("注销旁路清理容器启动失败：%s（推理容器/卷请手动清理）", e)

    # ---- 升级主流程 ----

    async def _dance(self, target: str, image_ref: str) -> UpgradeOutcome | None:
        """执行完整替换流程；返回 None 表示无需上报（预检忽略类）。成功路径在上报后
        rm -f 自己（进程终止，不返回）。"""
        name = os.environ.get("LLMPOD_NODE_CONTAINER_NAME", "")
        old_name = f"{name}-old" if name else ""

        # 0 预检
        if not name:
            return UpgradeOutcome(
                "not_supported",
                detail="缺 LLMPOD_NODE_CONTAINER_NAME（存量节点请重跑引导脚本获得自升级能力）",
                target_version=target,
            )
        if _version_at_most(target, llmpod.__version__):
            log.info("node_upgrade: target %s ≤ 自身 %s，忽略（只向前升级）", target, llmpod.__version__)
            return None
        if not is_registry_ref(image_ref):
            return UpgradeOutcome(
                "not_supported",
                detail=f"image_ref 不是 registry ref: {image_ref}（本地 tag 请 save/load 手动升级）",
                target_version=target,
            )

        try:
            cli = get_docker_client()
        except Exception as e:
            return UpgradeOutcome(
                "unknown_error", error_class="docker_unavailable", detail=repr(e)[:200], target_version=target
            )

        # 防御性清理：残留 -old 只可能是上次升级中断的已停容器；running 的是异常
        # 现场（两个同名体系在线），不敢自动处置——持有者是本进程时（上次中断后
        # 自己活在 -old 名下）更不能建议 docker rm，那删掉的是活节点
        stale = await _get_or_none(cli, old_name)
        if stale is not None:
            my_id = await asyncio.to_thread(_self_container_id, cli)
            if stale.status != "running":
                await asyncio.to_thread(stale.remove, force=True)
                log.info("已清理残留的已停 %s", old_name)
            elif stale.id == my_id:
                return UpgradeOutcome(
                    "not_supported",
                    detail=f"本进程活在 {old_name} 名下（上次升级中断现场）：请 docker restart 本容器，由启动恢复收敛后再升级",
                    target_version=target,
                )
            else:
                return UpgradeOutcome(
                    "not_supported",
                    detail=f"{old_name} 正在运行（上次升级中断现场），需人工确认后 docker rm",
                    target_version=target,
                )

        dance_token = uuid.uuid4().hex
        t0 = time.time()
        new_id: str | None = None  # create 成功前恒为 None——回滚只认它
        # try 从维护模式进入就开始包：suspend/snapshot 期间的 await 同样是取消点
        # （通道关闭即取消本协程），漏包会让节点永久卡维护态。
        try:
            # 1 维护模式：取消在跑任务 + 快照 + 关 sqlite（新容器独占数据卷，无锁竞争）
            if self._suspend is not None:
                try:
                    await self._suspend()
                except Exception:
                    # suspend 可能已部分生效（_maintenance=True + 已取消任务），漏 resume 会让
                    # 节点永久卡在维护态（run loop 跳过恢复、sqlite 关闭拒新任务）。尽力恢复。
                    log.exception("维护模式进入失败，中止升级（容器不动）")
                    await self._safe_resume()
                    return UpgradeOutcome("unknown_error", error_class="suspend_failed", target_version=target)
            try:
                await asyncio.to_thread(self._make_snapshot)
                self._remove_ready_marker()
            except Exception as e:
                await self._safe_resume()
                return UpgradeOutcome(
                    "unknown_error", error_class="snapshot_failed", detail=repr(e)[:200], target_version=target
                )

            me = await asyncio.to_thread(cli.containers.get, name)
            attrs = me.attrs
            hc, cfg = attrs["HostConfig"], attrs["Config"]

            # 2 pull（本地有则跳过；存在性检查 catch 全部异常按需 pull 处理——
            # 怪 ref 会抛 APIError(400) 而非 ImageNotFound）
            need_pull = True
            try:
                await asyncio.to_thread(cli.images.get, image_ref)
                need_pull = False
                log.info("镜像已在本地，跳过 pull: %s", image_ref)
            except Exception:
                pass
            if need_pull:
                log.info("pulling %s ...", image_ref)
                try:
                    await asyncio.to_thread(cli.images.pull, image_ref)
                except Exception as e:
                    self._discard_snapshot()
                    await self._safe_resume()
                    return UpgradeOutcome(
                        "pull_failed", error_class=classify_pull_error(e), detail=str(e)[:300], target_version=target
                    )

            # 3 rename（相位先落盘后动手：rename 已生效而磁盘无记录的窗口被打断
            # 后，boot 恢复无从收敛）
            self._write_state(PHASE_RENAMED, target, image_ref, None, t0, dance_token)
            await asyncio.to_thread(me.rename, old_name)
            await asyncio.to_thread(me.reload)
            log.info("已 rename → %s（%s）", old_name, me.status)

            # 4 create：整块 HostConfig 回放 + 升级 env 覆写 + 升级令牌 + 挂载断言
            env = [e for e in (cfg.get("Env") or []) if not e.startswith(f"{_IMAGE_ENV_KEY}=")]
            env.append(f"{_IMAGE_ENV_KEY}={image_ref}")
            env.append(f"{DANCE_TOKEN_ENV}={dance_token}")
            # creating 相位先落盘：create 调用进行中被打断时，{name} 可能已被
            # 无主半成品占住而无 new_id 可查——boot 恢复按令牌 env 识别处置
            self._write_state(PHASE_CREATING, target, image_ref, None, t0, dance_token)
            try:
                created = await asyncio.to_thread(
                    cli.api.create_container,
                    name=name,
                    image=image_ref,
                    command=cfg.get("Cmd") or None,
                    entrypoint=cfg.get("Entrypoint") or None,
                    environment=env,
                    labels=cfg.get("Labels") or {},
                    host_config=dict(hc),
                    # Healthcheck 属 Config 不属 HostConfig：引导脚本的 --no-healthcheck
                    # （Test:["NONE"]）在 Config.Healthcheck 里，漏传会让新容器回退
                    # 镜像内置探活（node 无 HTTP，永久 unhealthy）。WorkingDir/User/
                    # StopSignal 等其余 Config 字段同属此列——用到即在下面显式加
                    healthcheck=cfg.get("Healthcheck") or None,
                )
            except Exception as e:
                # create 失败（名字冲突/daemon 拒绝）：无新容器，rename 回原名即可
                return await self._abort(
                    cli,
                    name,
                    old_name,
                    None,
                    UpgradeOutcome(
                        "create_failed",
                        error_class=type(e).__name__,
                        detail=str(e)[:200] or None,
                        target_version=target,
                    ),
                )
            new_id = created["Id"]
            self._write_state(PHASE_CREATED, target, image_ref, new_id, t0, dance_token)

            new = await asyncio.to_thread(cli.containers.get, new_id)
            expected = {(m.get("Source"), m.get("Destination")) for m in attrs.get("Mounts") or []}
            actual = {(m.get("Source"), m.get("Destination")) for m in new.attrs.get("Mounts") or []}
            if expected != actual:
                # 零挂载/残缺挂载的静默残废容器（无 sock/数据卷）——按 create 失败回滚
                return await self._abort(
                    cli,
                    name,
                    old_name,
                    new_id,
                    UpgradeOutcome(
                        "create_failed",
                        error_class="mount_replay_mismatch",
                        detail=f"挂载回放不一致: 期望 {sorted(map(str, expected))} 实得 {sorted(map(str, actual))}",
                        target_version=target,
                    ),
                )
            log.info("新容器已创建 %s（挂载校验通过）", new_id[:12])

            # 5 start
            try:
                await asyncio.to_thread(new.start)
            except Exception as e:
                return await self._abort(
                    cli,
                    name,
                    old_name,
                    new_id,
                    UpgradeOutcome(
                        "start_failed", error_class=type(e).__name__, detail=str(e)[:200] or None, target_version=target
                    ),
                )
            self._write_state(PHASE_STARTED, target, image_ref, new_id, t0, dance_token)
            log.info("新容器已启动，等待 ready marker（target=%s）...", target)

            # 6 等就绪（marker 判定真正可用；exited/restarting 早判崩溃）
            ready, error_class, logs = await self._wait_ready(new, t0, target, dance_token)
            if not ready:
                result = "start_failed" if (error_class or "").startswith("crashed") else "not_ready"
                return await self._abort(
                    cli,
                    name,
                    old_name,
                    new_id,
                    UpgradeOutcome(result, error_class=error_class, detail=logs, target_version=target),
                )

            # 7a 成功：清状态/快照 → 上报（尽力）→ rm -f 自己（进程在此终止）
            log.info("新容器就绪，升级成功 → 清理并 rm -f self")
            self._clear_state()
            self._discard_snapshot()
            # 送出成功才等 flush（给 WS 发送缓冲留出落盘时间，随后进程即退出）
            if await self._report(UpgradeOutcome("success", target_version=target)):
                await asyncio.sleep(_REPORT_FLUSH_SECONDS)
            try:
                await asyncio.to_thread(me.remove, force=True)
            except Exception:
                # rm 失败也必须退场：落进下方兜底会让老容器退出维护、与新容器双活
                # 共写同一数据卷。硬退出后 restart 拉起 → retire_if_superseded 按
                # 接管者在场自清理收敛
                log.exception("rm -f 老容器失败（退出进程，交 restart/接管收敛）")
            os._exit(0)  # remove 后进程通常已死；防御 daemon 异步删除
        except asyncio.CancelledError:
            # 通道关闭会取消本协程（upgrade 处理器跑在旧 channel 的后台任务里）——
            # 升级成功临界点 manager 踢旧通道、manager 重启断连都会走到这里。
            # CancelledError 不是 Exception，直接穿透会让节点永久卡在维护态
            # （_maintenance=True + sqlite 已关，进程未重启无 boot 恢复路径）。
            # 落相位状态 + 退出维护后重抛取消：新容器已接管时由 retire_if_superseded
            # / 对方 boot 恢复收敛，rename 未发生时本容器仍是唯一节点、恢复服务。
            try:
                self._write_state(f"{PHASE_CANCELLED}:{new_id or ''}", target, image_ref, new_id, t0, dance_token)
            except Exception:
                log.exception("取消路径写状态失败（交人工/重启恢复）")
            try:
                await self._safe_resume()
            except Exception:
                log.exception("取消路径退出维护失败（需人工介入）")
            raise
        except Exception as e:
            # 兜底铁律：什么都不动（不删新容器不 rename）。相位已逐步落盘，交 boot
            # 恢复收敛；上报提示人工/重启收敛。
            self._write_state(f"error:{type(e).__name__}", target, image_ref, new_id, t0, dance_token)
            log.exception("升级未预期异常（容器不动，交 boot 恢复收敛）")
            await self._safe_resume()
            return UpgradeOutcome(
                "unknown_error",
                detail=(repr(e)[:200] + "；请 docker restart 本节点容器以触发启动恢复"),
                target_version=target,
            )

    # ---- 等就绪 / 回滚 ----

    async def _report(self, outcome: UpgradeOutcome) -> bool:
        """best-effort 上报（WS 断了/回调抛异常都不影响升级语义）；返回是否实际送出。"""
        if self._report_result is None:
            return False
        try:
            await self._report_result(outcome)
            return True
        except Exception:
            log.debug("upgrade result report failed (best-effort): %s", outcome.result)
            return False

    async def _abort(
        self, cli, name: str, old_name: str, new_id: str | None, outcome: UpgradeOutcome
    ) -> UpgradeOutcome:
        """失败分支统一收敛：回滚 → （回滚完整才）清状态 → 退出维护，返回携带日志尾的 outcome。

        回滚详情（新容器日志尾）并入 ``outcome.detail`` 供上报。回滚三步任一步
        未完成（删新容器/还原快照/rename 回原名）→ **保留状态文件与快照、留在
        维护态**：此刻清状态等于删掉 boot 恢复的唯一入口（状态文件就是它读的），
        退出维护继续写库则会让下次启动的快照还原丢掉这段写入——维持现状交
        docker restart 收敛最稳。
        """
        detail, rollback_ok = await self._rollback(cli, name, old_name, new_id)
        if rollback_ok:
            self._clear_state()
            await self._safe_resume()
        else:
            outcome.error_class = outcome.error_class or "rollback_incomplete"
        restart_hint = "" if rollback_ok else "回滚未完成，请 docker restart 本容器由启动恢复收敛"
        outcome.detail = "; ".join(x for x in (detail, outcome.detail, restart_hint) if x) or None
        return outcome

    async def _wait_ready(
        self, new, t0: float, target_version: str = "", token: str = ""
    ) -> tuple[bool, str | None, str | None]:
        """轮询 ready marker；新容器 exited/dead/restarting 提前失败。

        marker 有效条件：``ts > t0``（t0 = 替换流程开始时间，排除上一次升级留下的旧
        marker）且 ``version`` 非空。带 ``token`` 时还要求 marker 是**本次升级新
        容器**写的——写出侧每次 $register_ack 都写（升级窗口内老容器 WS 抖线重连
        也会写，内容是旧版本+新鲜时间戳），不带本次升级令牌的一律忽略继续等，否则
        健康新容器会被误判 version_mismatch 回滚掉。版本闸仍保留：本地已有同名
        tag 的旧镜像时（构建/推送纪律脱节让 tag 与内容不符），新容器跑的是旧代码，
        marker 版本对不上即判失败交回滚，不能让「升级成功」名不副实地糊过去。
        """
        ready_path = self._data_dir() / READY_FILE
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            await asyncio.to_thread(new.reload)
            status = new.status
            if status in ("exited", "dead", "restarting", "created"):
                rc = (new.attrs.get("State") or {}).get("ExitCode")
                logs = await self._tail_logs(new)
                return False, f"crashed(status={status}, exit={rc})", logs
            if ready_path.exists():
                try:
                    marker = json.loads(ready_path.read_text())
                except Exception:
                    marker = {}
                # 写出者甄别：令牌不匹配=别人（老容器）写的，不算新容器就绪证据
                writer_is_new = not token or marker.get("dance") == token
                if writer_is_new:
                    version = str(marker.get("version") or "")
                    if marker.get("ts", 0) > t0 and version:
                        if not target_version or _version_at_most(target_version, version):
                            return True, None, None
                        log.warning(
                            "新容器 ready marker 版本 %s 未追平 target %s（本地 tag 与内容不符？）",
                            version,
                            target_version,
                        )
                        logs = await self._tail_logs(new)
                        return False, f"version_mismatch(marker={version}, target={target_version})", logs
            await asyncio.sleep(READY_POLL_INTERVAL)
        return False, "ready_timeout", await self._tail_logs(new)

    async def _rollback(self, cli, name: str, old_name: str, new_id: str | None) -> tuple[str, bool]:
        """失败回滚：按 new_id 删新容器 → 恢复 sqlite 快照 → rename 回原名。

        只认 create 时捕获的 new_id，绝不按名字反查（名字此刻可能仍指着老容器）。
        返回 ``(新容器日志尾, 回滚是否完整)``——任一步未完成返回 False，调用方
        保留状态文件与快照交 boot 恢复收敛（「boot 恢复会收敛」的前提是状态
        文件还在）。
        """
        detail = ""
        ok = True
        if new_id is not None:
            try:
                new = await asyncio.to_thread(cli.containers.get, new_id)
                detail = await self._tail_logs(new)
                await asyncio.to_thread(new.remove, force=True)
                log.info("已回滚：删除新容器 %s", new_id[:12])
            except NotFound:
                pass
            except Exception:
                ok = False
                log.exception("回滚删新容器失败（残留 %s，boot 恢复会收敛）", new_id[:12])
        try:
            await asyncio.to_thread(self._restore_snapshot)
        except Exception:
            ok = False
            log.exception("sqlite 快照恢复失败（boot 恢复会再试）")
        try:
            me = await asyncio.to_thread(cli.containers.get, old_name)
            await asyncio.to_thread(me.rename, name)
            await asyncio.to_thread(me.reload)
            log.info("已回滚：rename 回 %s（%s）", name, me.status)
        except Exception:
            ok = False
            log.exception("rename 回原名失败：%s 不存在或不可 rename（交人工/启动恢复）", old_name)
        return detail, ok

    async def _safe_resume(self) -> None:
        if self._resume is not None:
            try:
                await self._resume()
            except Exception:
                log.exception("退出维护模式失败（sqlite 重开异常，需人工介入）")

    # ---- boot 恢复 ----

    async def retire_if_superseded(self) -> None:
        """名字持有者另有其人 → 我是残活前任，按自身 Id rm -f 自己（每次注册成功后自检）。

        覆盖状态文件管不了的窗口：升级「等就绪」阶段整机重启，新容器 boot 恢复
        （平凡路径）先跑完清掉 upgrade_state.json，前任重启后找不到状态文件、
        以正常节点身份在 ``-old`` 名下复活——两进程共享 node_id 与数据卷，互踢
        channel 不自愈（实测 ~6 轮/45s 的 duplicate kick 往复互踢）。

        判定：docker 不允许重名，env 名下出现**异 Id** 的 running 持有者只可能
        来自 rename 替换后的接管。删自己只认自身容器 Id，绝不按名字（名字在
        接管者手里）。替换流程进行中不触发（_busy：生死由替换流程管理，等 ready marker
        确认后才退位，提前退位会丢失败回滚）。自身 Id 解析不出 / docker 异常
        一律 fail-open，交既有上报/人工通道。
        """
        if self._busy:
            return
        name = os.environ.get("LLMPOD_NODE_CONTAINER_NAME", "")
        try:
            cli = get_docker_client()
        except Exception:
            log.exception("残活前任自检：docker 不可用（fail-open，按正常逻辑继续）")
            return
        my_id = await asyncio.to_thread(_self_container_id, cli)
        if not name or my_id is None:
            return
        try:
            holder = await _get_or_none(cli, name)
            if holder is None or holder.status != "running" or holder.id == my_id:
                return
            log.error(
                "容器名 %s 的持有者是 %s（本进程 %s）：判定为升级残留的残活前任容器，自清理",
                name,
                str(holder.id)[:12],
                my_id[:12],
            )
            me = await asyncio.to_thread(cli.containers.get, my_id)
            await asyncio.to_thread(me.remove, force=True)
            os._exit(0)  # remove 后进程通常已死；防御 daemon 异步删除
        except Exception:
            log.exception("残活前任自检失败（fail-open，按正常逻辑继续）")

    async def recover_on_boot(self) -> bool:
        """启动残留恢复：数据卷存在 upgrade_state.json → 按相位收敛。无残留返回 False。

        身份判定（升级令牌，env ``LLMPOD_UPGRADE_DANCE_TOKEN``，名称保留）：create 时给新容器
        注入该 env 并落盘 state——env 匹配者即新容器。host 网络下 hostname 是宿主名，不能用
        hostname==容器 id 的常规手法。

        - 我是新容器 → 升级实际成功，清理状态/快照即可（老容器若残活，看到我的
          ready marker 会自行 rm -f）
        - 我是老容器（-old 名下）→ 等新容器就绪（marker，宽限 READY_TIMEOUT）：
          就绪 → rm -f self 功成身退；未就绪 → rm 半成品（只认落盘 new_id）+
          恢复快照 + rename 回原名

        另一道闸：状态是前任节点注销时漏下的（token 不匹配 + 本容器不在 -old 名下
        且名字归属者不是本容器 Id）→ 按前任的相位「恢复」会拿前任快照盖掉本节点
        的任务库。凡身份对不上的残留一律只清文件，不做任何 docker/快照操作。
        """
        state_path = self._data_dir() / STATE_FILE
        if not state_path.exists():
            return False
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            log.exception("upgrade_state.json 损坏，删除并按正常启动处理")
            state_path.unlink(missing_ok=True)
            return False

        phase = str(state.get("phase") or "")
        new_id = state.get("new_id")
        ts = float(state.get("ts") or 0)
        target = str(state.get("target") or "")
        token = state.get("token")
        log.warning("发现升级残留 phase=%s new_id=%s（启动恢复收敛）", phase, str(new_id)[:12])

        if token and os.environ.get(DANCE_TOKEN_ENV) == token:
            # 本容器是升级新容器（升级令牌匹配）。只清状态文件（让旧容器不再按相位收敛），
            # **快照留给旧容器处置**：成功路径 7a 由旧容器 discard、失败路径 7b 由旧容器
            # restore。此处 discard 会过早删快照——新容器若在 $register_ack 写 ready marker
            # 前崩溃，旧容器回滚时已无快照可恢复（恰恰丢在需要它的失败场景里）。
            log.info("升级令牌匹配：本容器即升级新容器，清理状态文件（快照留待旧容器确认后处置）")
            self._clear_state()
            return False

        if not os.environ.get("LLMPOD_NODE_CONTAINER_NAME"):
            log.error("启动恢复：缺 LLMPOD_NODE_CONTAINER_NAME，无法自动收敛，需人工处理 %s", state_path)
            return True

        # 老容器身份核对：替换流程中老容器必然活在 {name}-old 名下（或整机重启后名字
        # 已被 rename 回 {name}——持有者 Id 是自己）。两者都不成立 = 状态文件属于
        # 上一任节点（注销漏清 / 复用数据卷重装），不是本节点的替换流程残留。
        name = os.environ["LLMPOD_NODE_CONTAINER_NAME"]
        try:
            cli = get_docker_client()
        except Exception:
            log.exception("启动恢复：docker 不可用，保留状态文件下次启动再试")
            return True
        my_id = await asyncio.to_thread(_self_container_id, cli)
        me_old = await _get_or_none(cli, f"{name}-old")
        me_named = await _get_or_none(cli, name)
        if not ((me_old is not None and me_old.id == my_id) or (me_named is not None and me_named.id == my_id)):
            log.warning(
                "启动恢复：状态文件身份对不上本容器（%s 下都不是自己），判定为前任节点残留，只清文件不收敛", name
            )
            self._clear_state()
            self._discard_snapshot()
            return False

        old_name = f"{name}-old"
        me = await _get_or_none(cli, old_name)

        try:
            if phase == PHASE_CREATING:
                await self._recover_creating(cli, name, me, my_id, token)
                return True
            new = await _get_or_none(cli, new_id) if new_id else None

            if phase.startswith(f"{PHASE_CANCELLED}:") and new is None:
                # 取消时还没 create（renamed 相位窗口）：rename 回原名回家
                if me is not None:
                    await asyncio.to_thread(me.rename, os.environ["LLMPOD_NODE_CONTAINER_NAME"])
                    log.info("恢复（cancelled 早相位）：rename 回原名")
            elif phase == PHASE_RENAMED and me is not None and new is None:
                # create 前死：无新容器，直接回家
                await asyncio.to_thread(me.rename, os.environ["LLMPOD_NODE_CONTAINER_NAME"])
                log.info("恢复（renamed 相位）：rename 回原名")
            else:
                # started/created/error 相位：新容器可能只是还没就绪——等 marker
                # （新容器要完成 python 启动 + WS 注册才写，宽限等齐；只认本次升级
                # 令牌写出者，老容器重连写的 marker 不算数）
                ready = False
                if new is not None:
                    ready = (await self._wait_ready(new, ts, target, token or ""))[0]
                if ready and me is not None:
                    log.info("恢复：新容器已就绪（升级实际成功）→ rm -f self 功成身退")
                    self._clear_state()
                    self._discard_snapshot()
                    await asyncio.to_thread(me.remove, force=True)
                    os._exit(0)
                # 未就绪 / 无新容器 → 清半成品 + 回家
                if new is not None:
                    try:
                        await asyncio.to_thread(new.remove, force=True)
                        log.info("恢复：已清理半成品新容器 %s", str(new_id)[:12])
                    except Exception:
                        log.exception("恢复：清理半成品失败（保留状态，下次启动再试）")
                        return True
                if me is not None:
                    await asyncio.to_thread(me.rename, os.environ["LLMPOD_NODE_CONTAINER_NAME"])
                    log.info("恢复：rename 回原名")
            # 快照还原只发生在**新容器存在过**的残留上（它启动可能写过库，还原是
            # 为撤销它的写入）；从未建出新容器的失败（pull/建容器前异常）库没人
            # 动过，还原反而拿替换流程开始时的旧快照盖掉此后正常运行的写入——只弃不还原
            if new_id:
                try:
                    await asyncio.to_thread(self._restore_snapshot)
                except Exception:
                    log.exception("恢复：sqlite 快照恢复失败（保留状态，下次启动再试）")
                    return True
            else:
                self._discard_snapshot()
            self._clear_state()
            log.info("启动恢复收敛完成（target=%s）", target)
            return True
        except Exception:
            log.exception("启动恢复异常（保留状态文件，下次启动再试）")
            return True

    async def _recover_creating(self, cli, name: str, me, my_id: str, token) -> None:
        """creating 相位收敛：create 调用进行中被打断，{name} 可能已被半成品占住。

        半成品带本次升级令牌 env（create 注入、daemon 建好了容器但我们没拿到
        new_id）——按令牌识别，带令牌的可清（它从未被 start：start 严格发生在
        created 相位落盘之后，任务库无人动过，快照只弃不还原）；不带令牌的持有
        者是异源容器，不敢动，保留状态交人工。"""
        holder = await _get_or_none(cli, name)
        if holder is not None and holder.id != my_id:
            holder_token = _container_env_token(holder)
            if token and holder_token == token:
                await asyncio.to_thread(holder.remove, force=True)
                log.info("恢复（creating 相位）：已清占名半成品 %s（升级令牌匹配）", str(holder.id)[:12])
            else:
                log.error(
                    "恢复（creating 相位）：%s 持有者不带本次升级令牌（异源容器），不敢自动处置，需人工处理", name
                )
                return  # 保留状态文件，下次启动再试
        if me is not None:
            await asyncio.to_thread(me.rename, name)
            log.info("恢复（creating 相位）：rename 回原名")
        self._discard_snapshot()
        self._clear_state()
        log.info("启动恢复收敛完成（creating 相位）")

    # ---- ready marker（新容器侧写，本类读/清）----

    def write_ready_marker(self, node_id: str) -> None:
        """$register_ack 后写 ready marker。每次注册都写（不只升级时）——老容器的
        等就绪轮询据此判定新容器真正可用；``version`` 供人工核对。升级新容器
        （env 带升级令牌）写的 marker 附 ``dance`` 字段，轮询侧据它与老容器重连
        写的 marker 区分。"""
        payload = {"version": llmpod.__version__, "ts": time.time(), "node_id": node_id}
        dance = os.environ.get(DANCE_TOKEN_ENV, "")
        if dance:
            payload["dance"] = dance
        path = self._data_dir() / READY_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
        except Exception:
            log.exception("ready marker 写入失败")

    def _remove_ready_marker(self) -> None:
        (self._data_dir() / READY_FILE).unlink(missing_ok=True)

    # ---- 文件辅助（数据卷路径 / sqlite 快照 / 相位落盘）----

    def _data_dir(self) -> Path:
        # node_task_db 形如 data/node_tasks.db——数据卷即其父目录（node_id/token 同处）
        return Path(self._settings.node_task_db).parent

    def _snapshot_path(self) -> Path:
        return Path(str(self._settings.node_task_db) + ".upgrade-bak")

    def _make_snapshot(self) -> None:
        src = Path(self._settings.node_task_db)
        if src.exists():
            self._data_dir().mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, self._snapshot_path())
            log.info("sqlite 快照: %s", self._snapshot_path().name)

    def _restore_snapshot(self) -> None:
        bak = self._snapshot_path()
        if bak.exists():
            shutil.copy2(bak, Path(self._settings.node_task_db))
            bak.unlink()
            log.info("sqlite 快照已恢复")

    def _discard_snapshot(self) -> None:
        self._snapshot_path().unlink(missing_ok=True)

    def _write_state(self, phase: str, target: str, image_ref: str, new_id: str | None, t0: float, token: str) -> None:
        path = self._data_dir() / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "target": target,
                    "image_ref": image_ref,
                    "new_id": new_id,
                    "token": token,
                    "ts": t0,
                    "updated_at": now_utc().isoformat(),
                },
                ensure_ascii=False,
            )
        )

    def _clear_state(self) -> None:
        (self._data_dir() / STATE_FILE).unlink(missing_ok=True)

    @staticmethod
    async def _tail_logs(container, lines: int = 10) -> str:
        try:
            raw = await asyncio.to_thread(container.logs, tail=lines)
            return raw.decode(errors="replace").strip()[-300:]
        except Exception:
            return ""
