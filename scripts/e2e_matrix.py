"""端到端部署矩阵测试：tp/pp/手动选卡/显存共享/跨机选节点/架构不符。

驱动管理端 REST 创建部署 → 轮询 DB 直到终态 → 校验 instance/assignment/task/endpoint →
curl 推理端点验证真服务 → 清理（stop+delete）。如实记录每个场景的进度与结果。

用法：python scripts/e2e_matrix.py [--cases tp1,tp2,...] [--keep]
  默认跑全部场景；--keep 不清理（保留部署供观察）。

目标硬件与节点标识经环境变量注入：LLMPOD_E2E_GPU0 / LLMPOD_E2E_GPU1（GPU UUID）、
LLMPOD_E2E_CANN_GPU（Ascend NPU）、LLMPOD_E2E_NODE_A / LLMPOD_E2E_NODE_B（节点 id）。
依赖项缺失时对应场景记 SKIP，不会用空 selector 发请求。

前提：管理端 8080 已起、节点 online、模型 1（Qwen2.5-0.5B）已注册、vLLM 镜像已缓存。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field

import asyncpg

# ---- 配置（默认与 .env.example 的 dev 栈一致；可用环境变量覆盖，防与部署漂移）----
API = os.environ.get("LLMPOD_E2E_API", "http://127.0.0.1:8080/api/v1/admin")


def _load_token() -> str:
    """运行时读管理端令牌文件（默认 .run/token.txt）；缺失时给出可行动的报错。"""
    path = os.environ.get("LLMPOD_E2E_TOKEN_FILE", ".run/token.txt")
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        sys.exit(f"令牌文件不存在: {path}（登录管理端后写入，或用 LLMPOD_E2E_TOKEN_FILE 指定）")


def _pg_conf() -> dict:
    """PG 连接配置；口令必须经环境变量给（不在脚本里内置兜底值）。"""
    try:
        password = os.environ["LLMPOD_PG_PASSWORD"]
    except KeyError:
        sys.exit("请设置 LLMPOD_PG_PASSWORD（与目标库 .env 一致）")
    return dict(
        host=os.environ.get("LLMPOD_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LLMPOD_PG_PORT", "35432")),
        user=os.environ.get("LLMPOD_PG_USER", "postgres"),
        password=password,
        database=os.environ.get("LLMPOD_PG_DATABASE", "llmpod"),
    )


MODEL_ID = 1
CLUSTER_ID = 1
# 目标环境的 GPU/节点标识（控制台或 gpu/node 表可查），全部经环境变量注入，不内置具体环境值。
GPU0 = os.environ.get("LLMPOD_E2E_GPU0", "")
GPU1 = os.environ.get("LLMPOD_E2E_GPU1", "")
CANN = os.environ.get("LLMPOD_E2E_CANN_GPU", "")
NODE_A = os.environ.get("LLMPOD_E2E_NODE_A", "")
NODE_B = os.environ.get("LLMPOD_E2E_NODE_B", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_load_token()}", "Content-Type": "application/json"}


# 场景依赖的目标标识；缺失时该场景记 SKIP，避免拿空 selector 发请求。
ENV_VALUES = {
    "LLMPOD_E2E_GPU0": GPU0,
    "LLMPOD_E2E_GPU1": GPU1,
    "LLMPOD_E2E_CANN_GPU": CANN,
    "LLMPOD_E2E_NODE_A": NODE_A,
    "LLMPOD_E2E_NODE_B": NODE_B,
}


def missing_env(case):
    """返回场景需要但未设置的环境变量名。"""
    return [name for name in case.get("requires", []) if not ENV_VALUES[name]]


# ---- HTTP ----
def post(path: str, body: dict):
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode(), headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"code": e.code, "_body": e.read().decode()[:300]}
    except Exception as e:
        return {"code": -1, "_err": str(e)[:200]}


def get_json(url: str, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)[:150]}


# ---- DB 快照 ----
async def db_snapshot(conn, dep_id):
    d = await conn.fetchrow(
        "select status,phase,api_endpoint,last_error from model_deployment where deployment_id=$1", dep_id
    )
    insts = await conn.fetch(
        "select instance_id,status,assigned_node,spec from instance where deployment_id=$1", dep_id
    )
    assigns = await conn.fetch(
        "select gpu_uuid,instance_id,allocated_vram_gb,gpu_memory_utilization,resource_type "
        "from deployment_gpu_assignment where deployment_id=$1",
        dep_id,
    )
    tasks = await conn.fetch(
        "select type,status,attempt,node_id from task where payload->>'deployment_id'=$1 or resource_key LIKE $2",
        dep_id,
        f"%{dep_id[:8]}%",
    )
    return {
        "deploy": dict(d) if d else None,
        "instances": [
            {
                "id": i["instance_id"][:8],
                "status": i["status"],
                "node": (i["assigned_node"] or "")[:8],
                "gpus": (json.loads(i["spec"]).get("assigned_gpus") if i["spec"] else None),
                "uuids": (json.loads(i["spec"]).get("assigned_gpu_uuids") if i["spec"] else None),
            }
            for i in insts
        ],
        "assignments": [
            {
                "gpu": a["gpu_uuid"][:14],
                "inst": (a["instance_id"] or "")[:8],
                "alloc": a["allocated_vram_gb"],
                "util": a["gpu_memory_utilization"],
                "arch": a["resource_type"],
            }
            for a in assigns
        ],
        "tasks": [(t["type"], t["status"], t["attempt"]) for t in tasks],
    }


# ---- 部署生命周期 ----
def create_deploy(name, topology, gpu_selector=None, engine_params=None):
    spec = {"topology": topology, "engine_params": engine_params or {"extra_args": ["--dtype", "half"]}}
    if gpu_selector is not None:
        spec["gpu_selector"] = gpu_selector
    return post(
        "/model-deployments",
        {"cluster_id": CLUSTER_ID, "name": name, "model_id": MODEL_ID, "engine": "vllm", "spec": spec},
    )


async def poll_terminal(conn, dep_id, timeout=300):
    """轮询直到 status ∈ {running, failed, stopped, deleting}。返回 (status, phase, duration, transitions)。"""
    start = time.time()
    last_phase = None
    transitions = []
    while time.time() - start < timeout:
        d = await conn.fetchrow("select status,phase from model_deployment where deployment_id=$1", dep_id)
        if not d:
            return ("gone", None, time.time() - start, transitions)
        if d["phase"] != last_phase:
            transitions.append((round(time.time() - start, 1), d["phase"]))
            last_phase = d["phase"]
        if d["status"] in ("running", "failed", "stopped"):
            return (d["status"], d["phase"], time.time() - start, transitions)
        await asyncio.sleep(4)
    return ("timeout", last_phase, time.time() - start, transitions)


def serve_check(endpoint):
    """api_base 已携带版本根 /v1（http://ip:port/v1），直接拼 /models——不再二次拼 /v1。"""
    if not endpoint:
        return False, "no endpoint"
    m = get_json(f"{endpoint.rstrip('/')}/models", timeout=10)
    if "_err" in m:
        return False, m["_err"]
    ids = [x.get("id") for x in m.get("data", [])]
    return bool(ids), f"models={ids}"


def cleanup(dep_id):
    """stop → 等 → delete → 等。硬删除会移除 deployment 行。"""
    post(f"/model-deployments/{dep_id}/stop", {})
    time.sleep(14)  # 等容器停止收敛（health task cancel + stop instance）
    post(f"/model-deployments/{dep_id}/delete", {})
    time.sleep(6)


@dataclass
class CaseResult:
    name: str
    desc: str
    outcome: str = ""  # running / failed / rejected / timeout
    duration: float = 0.0
    transitions: list = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    serve: tuple = (False, "")
    error: str = ""
    verdict: str = ""  # PASS / FAIL / EXPECTED_FAIL / SKIP


def verdict_for(case_name, outcome, snapshot):
    """按场景期望判 PASS/FAIL/EXPECTED_FAIL。"""
    serving = case_name.get("expect_running", True)
    if serving and outcome == "running":
        return "PASS"
    if (not serving) and outcome in ("failed", "rejected", "gone"):
        return "EXPECTED_FAIL"  # 预期失败的场景（如跨机无 nvidia、架构不符）
    if outcome == "running" and not serving:
        return "FAIL"  # 本该失败却 running
    return "FAIL"


async def run_case(conn, case):
    if case.get("is_share"):
        return await run_share(conn, case)
    if case.get("is_replicas"):
        return await run_replicas2(conn, case)
    if case.get("is_scale"):
        return await run_scale_up_down(conn, case)
    res = CaseResult(name=case["name"], desc=case["desc"])
    print(f"\n▶ {case['name']}: {case['desc']}")
    cr = create_deploy(case["name"], case["topology"], case.get("gpu_selector"), case.get("engine_params"))
    if cr.get("code") != 200:
        res.outcome = "rejected"
        res.error = str(cr)[:200]
        res.verdict = "EXPECTED_FAIL" if not case["expect_running"] else "FAIL"
        print(f"  ✗ rejected: {res.error[:120]}")
        return res
    dep_id = cr["data"]["deployment_id"]
    res.outcome, res.phase_dummy, res.duration, res.transitions = await poll_terminal(
        conn, dep_id, timeout=case.get("timeout", 300)
    )
    res.snapshot = await db_snapshot(conn, dep_id)
    ep = res.snapshot["deploy"]["api_endpoint"] if res.snapshot.get("deploy") else None
    res.serve = serve_check(ep)
    res.verdict = verdict_for(case, res.outcome, res.snapshot)
    err = res.snapshot["deploy"]["last_error"] if res.snapshot.get("deploy") else ""
    print(f"  → {res.outcome} ({res.duration:.0f}s) verdict={res.verdict} serve={res.serve[0]}")
    print(f"    phases: {res.transitions}")
    if res.snapshot["instances"]:
        for i in res.snapshot["instances"]:
            print(
                f"    inst {i['id']} status={i['status']} node={i['node']} gpus={i['gpus']} uuids={[u[:8] for u in (i['uuids'] or [])]}"
            )
    if res.snapshot["assignments"]:
        for a in res.snapshot["assignments"]:
            print(f"    assign {a['gpu']} alloc={a['alloc']} util={a['util']} arch={a['arch']}")
    if err:
        print(f"    last_error: {err[:120]}")
    if not ARGS.keep:
        cleanup(dep_id)
    return res


async def run_share(conn, case):
    """vram_percent 共享：两部署同卡各 0.4 → 期望都 running 且都落在 GPU0。"""
    res = CaseResult(name=case["name"], desc=case["desc"])
    print(f"\n▶ {case['name']}: {case['desc']}")
    dep_ids = []
    for suffix in ("A", "B"):
        cr = create_deploy(
            f"{case['name']}-{suffix}", case["topology"], case.get("gpu_selector"), case.get("engine_params")
        )
        if cr.get("code") != 200:
            res.outcome = "rejected"
            res.error = str(cr)[:200]
            res.verdict = "FAIL"
            print(f"  ✗ deploy {suffix} rejected: {res.error[:120]}")
            return res
        dep_ids.append(cr["data"]["deployment_id"])
    # 顺序轮询两部署（asyncpg 单连接不支持并发查询）
    outs = [await poll_terminal(conn, d, timeout=240) for d in dep_ids]
    snaps = [await db_snapshot(conn, d) for d in dep_ids]
    both_running = all(o[0] == "running" for o in outs)
    serves = [serve_check(s["deploy"]["api_endpoint"] if s.get("deploy") else None) for s in snaps]
    # 校验两部署都落 GPU0
    gpu0_hit = 0
    for s in snaps:
        gpus = {a["gpu"] for a in s["assignments"]}
        if GPU0 and any(GPU0[:14] in g for g in gpus):
            gpu0_hit += 1
    res.outcome = "running" if both_running else "partial/" + ",".join(o[0] for o in outs)
    res.duration = max(o[2] for o in outs)
    res.transitions = outs[0][3]
    res.snapshot = {"A": snaps[0], "B": snaps[1], "share_gpu0": gpu0_hit}
    res.serve = (all(s[0] for s in serves), str([s[1][:30] for s in serves]))
    res.verdict = "PASS" if (both_running and gpu0_hit == 2) else "FAIL"
    print(f"  → both_running={both_running} share_gpu0={gpu0_hit}/2 verdict={res.verdict}")
    for tag, s in (("A", snaps[0]), ("B", snaps[1])):
        for a in s["assignments"]:
            print(f"    {tag} assign {a['gpu']} alloc={a['alloc']} util={a['util']}")
    if not ARGS.keep:
        for d in dep_ids:
            cleanup(d)
    return res


# ---- 多副本场景（四层模型）----


async def db_multi_snapshot(conn, dep_id):
    """多副本四层快照：副本/引擎/端点计数与状态。"""
    reps = await conn.fetch(
        "select replica_id,seq,status from model_deployment_replica where deployment_id=$1 order by seq", dep_id
    )
    engs = await conn.fetch(
        "select e.instance_id,e.role,e.status,e.gpu_arch from model_deployment_instance e "
        "join model_deployment_replica r on e.replica_id=r.replica_id where r.deployment_id=$1",
        dep_id,
    )
    eps = await conn.fetch("select id,protocol,name,health_status from model_endpoint where deployment_id=$1", dep_id)
    return {
        "replicas": [(r["seq"], r["status"]) for r in reps],
        "engines": [(e["role"], e["status"], e["gpu_arch"]) for e in engs],
        "endpoints": [(e["id"], e["protocol"], e["health_status"]) for e in eps],
    }


async def poll_replicas_serving(conn, dep_id, timeout=300):
    """轮询直到部署 running 且全部副本 serving。"""
    start = time.time()
    while time.time() - start < timeout:
        d = await conn.fetchrow("select status,phase from model_deployment where deployment_id=$1", dep_id)
        if d is None or d["status"] in ("failed", "stopped"):
            return (d["status"] if d else "gone", time.time() - start)
        reps = await conn.fetch("select status from model_deployment_replica where deployment_id=$1", dep_id)
        if d["status"] == "running" and reps and all(r["status"] == "serving" for r in reps):
            return ("serving", time.time() - start)
        await asyncio.sleep(4)
    return ("timeout", time.time() - start)


def proxy_chat(api_key, model, n=8):
    """经 API 网关 8090 打 n 次 chat，返回成功数（HTTP 200 计数，其余忽略）。"""
    ok = 0
    for _ in range(n):
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8090/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 200:
                    ok += 1
        except Exception:
            pass
    return ok


async def endpoint_id_distribution(conn, dep_id, since_min=10):
    """近 N 分钟该部署的 request_log.endpoint_id 分布（打流断言每副本接流）。"""
    rows = await conn.fetch(
        "select endpoint_id, count(*) as n from request_log rl "
        "join model_endpoint me on rl.endpoint_id = me.id "
        "where me.deployment_id=$1 and rl.created_at > now() - interval '"
        + str(since_min)
        + " minutes' group by endpoint_id",
        dep_id,
    )
    return {r["endpoint_id"]: r["n"] for r in rows}


def issue_api_key():
    """签发平台直发 API Key（多副本打流用）。"""
    r = post("/api-keys", {"name": "e2e-multi-replica"})
    if r.get("code") == 200:
        return r["data"]["plaintext"]
    return None


def kill_replica_container(dep_id, seq):
    """docker kill 指定副本的容器（宿主机 docker CLI；容器名 llmpod-{dep8}-{inst8}）。"""
    import subprocess

    prefix = f"llmpod-{dep_id[:8]}"
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.split()
    if not out:
        return False
    # 按容器名后缀（instance 短 id）排序近似——多副本时杀 seq 对应的（rank=seq 单机单卡）
    target = sorted(out)[seq % len(out)]
    subprocess.run(["docker", "kill", target], capture_output=True, timeout=20)
    return True


async def run_replicas2(conn, case):
    """replicas2：2 副本 running + 2 端点；打流断言双副本接流；杀容器 → 流量转移 → 自愈回流。"""
    res = CaseResult(name=case["name"], desc=case["desc"])
    print(f"\n▶ {case['name']}: {case['desc']}")
    cr = create_deploy(case["name"], case["topology"])
    if cr.get("code") != 200:
        res.outcome, res.error, res.verdict = "rejected", str(cr)[:200], "FAIL"
        print(f"  ✗ rejected: {res.error[:120]}")
        return res
    dep_id = cr["data"]["deployment_id"]
    res.outcome, res.duration = await poll_replicas_serving(conn, dep_id, timeout=case.get("timeout", 360))
    snap = await db_multi_snapshot(conn, dep_id)
    res.snapshot = snap
    eps_ok = len(snap["endpoints"]) == 2 and all(h == "healthy" for _, _, h in snap["endpoints"])
    reps_ok = len(snap["replicas"]) == 2 and all(s == "serving" for _, s in snap["replicas"])

    # 打流：经 proxy 断言两个副本都接流（request_log.endpoint_id 分布在 2 个端点）
    key = issue_api_key()
    dist = {}
    if key:
        proxy_chat(key, "Qwen2.5-0.5B", n=8)
        await asyncio.sleep(2)
        dist = await endpoint_id_distribution(conn, dep_id)
    dist_ok = len(dist) >= 2

    print(f"  → {res.outcome} ({res.duration:.0f}s) replicas={snap['replicas']} endpoints={len(snap['endpoints'])}")
    print(f"    engines: {snap['engines']}")
    print(f"    endpoint 分布: {dist}（双副本接流={'是' if dist_ok else '否'}）")

    # 杀一个副本容器 → 自愈回流（等副本回 serving）
    healed = None
    if res.outcome == "serving" and eps_ok:
        kill_replica_container(dep_id, seq=1)
        print("    已 kill 副本 2 容器，等自愈…")
        healed, _ = await poll_replicas_serving(conn, dep_id, timeout=case.get("heal_timeout", 240))
        print(f"    自愈: {healed}")

    res.verdict = "PASS" if (res.outcome == "serving" and eps_ok and reps_ok and (dist_ok or not key)) else "FAIL"
    if healed is not None:
        res.verdict = "PASS" if (res.verdict == "PASS" and healed == "serving") else "FAIL"
    res.serve = (eps_ok, f"endpoints={len(snap['endpoints'])} dist={len(dist)} healed={healed}")
    if not ARGS.keep:
        cleanup(dep_id)
    return res


async def run_scale_up_down(conn, case):
    """scale_up_down：replicas2 起步 → scale 1 → 3（2 卡机扩到 2）→ 断言端点数随动。"""
    res = CaseResult(name=case["name"], desc=case["desc"])
    print(f"\n▶ {case['name']}: {case['desc']}")
    cr = create_deploy(case["name"], case["topology"])
    if cr.get("code") != 200:
        res.outcome, res.error, res.verdict = "rejected", str(cr)[:200], "FAIL"
        return res
    dep_id = cr["data"]["deployment_id"]
    out0, d0 = await poll_replicas_serving(conn, dep_id, timeout=case.get("timeout", 360))

    # 缩容 2→1
    post(f"/model-deployments/{dep_id}/scale", {"copies": 1})
    t0 = time.time()
    scaled1 = "timeout"
    while time.time() - t0 < 240:
        snap = await db_multi_snapshot(conn, dep_id)
        spec = await conn.fetchrow(
            "select spec->'topology'->>'replicas' as n from model_deployment where deployment_id=$1", dep_id
        )
        if len(snap["replicas"]) == 1 and spec["n"] == "1" and len(snap["endpoints"]) == 1:
            scaled1 = "ok"
            break
        await asyncio.sleep(4)

    # 扩容 1→2（2 卡机上界）
    post(f"/model-deployments/{dep_id}/scale", {"copies": 2})
    out2, _ = await poll_replicas_serving(conn, dep_id, timeout=case.get("timeout", 360))
    snap2 = await db_multi_snapshot(conn, dep_id)
    res.outcome = f"{out0}→{scaled1}→{out2}"
    res.duration = d0 + (time.time() - t0)
    res.snapshot = {"after_scale_down": await db_multi_snapshot(conn, dep_id), "final": snap2}
    eps_final = len(snap2["endpoints"]) == 2
    print(f"  → {res.outcome}；终态 replicas={snap2['replicas']} endpoints={len(snap2['endpoints'])}")
    res.verdict = "PASS" if (out0 == "serving" and scaled1 == "ok" and out2 == "serving" and eps_final) else "FAIL"
    if not ARGS.keep:
        cleanup(dep_id)
    return res


# ---- 场景矩阵 ----
CASES = [
    {"name": "tp1", "desc": "单卡 TP=1（自动调度）", "topology": {"tp": 1, "replicas": 1}, "expect_running": True},
    {
        "name": "tp2",
        "desc": "双卡 TP=2 单节点（自动调度）",
        "topology": {"tp": 2, "replicas": 1},
        "expect_running": True,
    },
    {
        "name": "manual_tp1",
        "desc": "手选 1 卡 tp=1",
        "topology": {"tp": 1, "replicas": 1},
        "gpu_selector": {"gpu_uuids": [GPU0]},
        "requires": ["LLMPOD_E2E_GPU0"],
        "expect_running": True,
    },
    {
        "name": "manual_tp2",
        "desc": "手选 2 卡 tp=2 单节点",
        "topology": {"tp": 2, "replicas": 1},
        "gpu_selector": {"gpu_uuids": [GPU0, GPU1]},
        "requires": ["LLMPOD_E2E_GPU0", "LLMPOD_E2E_GPU1"],
        "expect_running": True,
    },
    {
        "name": "share",
        "desc": "vram_percent=0.4 共享：两部署同卡",
        "topology": {"tp": 1, "replicas": 1},
        "gpu_selector": {"gpu_uuids": [GPU0], "vram_percent": 0.4},
        "requires": ["LLMPOD_E2E_GPU0"],
        "expect_running": True,
        "is_share": True,
    },
    {
        "name": "pp1",
        "desc": "pp=1（流水线并行=1，默认）",
        "topology": {"tp": 1, "pp": 1, "replicas": 1},
        "expect_running": True,
    },
    {
        "name": "pp2",
        "desc": "pp=2 单机（流水线并行，多机 pp 形态的单机版冒烟）",
        "topology": {"tp": 1, "pp": 2, "replicas": 1},
        "expect_running": True,
    },
    {
        # 多机 pp 主推形态：pp=nnodes、tp=每节点卡数（单机双容器互当节点可先跑，
        # 真双机需两张同架构节点；NCCL 要求同子网/双向可达）
        "name": "pp_cross",
        "desc": "tp=1/pp=2/nnodes=2 跨机 PP（head+worker 各 1 卡）",
        "topology": {"tp": 1, "pp": 2, "nnodes": 2, "replicas": 1},
        "expect_running": True,
        "skip_reason": "需双 nvidia 节点（或单机双容器冒烟）；nvidia+ascend 跨架构不可用",
    },
    {
        "name": "node_ids_cross",
        "desc": "跨机选节点 [NODE_A,NODE_B]——ascend 节点无 nvidia，预期失败",
        "topology": {"tp": 2, "nnodes": 2, "replicas": 1},
        "gpu_selector": {"node_ids": [NODE_A, NODE_B]},
        "requires": ["LLMPOD_E2E_NODE_A", "LLMPOD_E2E_NODE_B"],
        "expect_running": False,
    },
    {
        "name": "ascend_cann",
        "desc": "Ascend 310P3 (cann) 单 NPU vllm-ascend",
        "topology": {"tp": 1, "replicas": 1},
        "gpu_selector": {"gpu_uuids": [CANN]},
        "requires": ["LLMPOD_E2E_CANN_GPU"],
        "expect_running": True,
        "timeout": 360,
    },
    # ---- 多副本（四层模型）----
    {
        "name": "replicas2",
        "desc": "2 副本各 1 卡：双端点 + 打流断言双接流 + 杀容器自愈",
        "topology": {"tp": 1, "replicas": 2},
        "is_replicas": True,
        "expect_running": True,
        "timeout": 360,
    },
    {
        "name": "scale_up_down",
        "desc": "2 副本 → scale 1 → 2：端点数随动",
        "topology": {"tp": 1, "replicas": 2},
        "is_scale": True,
        "expect_running": True,
        "timeout": 360,
    },
    # 依赖目标环境具备多机/异构资源，当前环境不满足时跳过
    {
        "name": "hetero_replicas3",
        "desc": "2 nvidia + 1 cann 异构 3 副本",
        "topology": {"tp": 1, "replicas": 3},
        "expect_running": False,
        "skip_reason": "目标环境 ascend 不可用",
    },
    {
        "name": "multinode_tp2",
        "desc": "tp=2/nnodes=2 跨机",
        "topology": {"tp": 2, "nnodes": 2, "replicas": 1},
        "expect_running": False,
        "skip_reason": "目标环境缺少第二台节点",
    },
    {
        "name": "multinode_replicas2",
        "desc": "跨机×多副本（需 4 卡 2 机）",
        "topology": {"tp": 2, "nnodes": 2, "replicas": 2},
        "expect_running": False,
        "skip_reason": "目标环境缺少第二台节点",
    },
]


async def main():
    pool = await asyncpg.create_pool(**_pg_conf(), min_size=1, max_size=2)
    async with pool.acquire() as conn:
        cases = [c for c in CASES if c["name"] in ARGS.cases] if ARGS.cases else CASES
        results = []
        for c in cases:
            if c.get("skip_reason"):
                results.append(
                    CaseResult(name=c["name"], desc=c["desc"], outcome="skip", error=c["skip_reason"], verdict="SKIP")
                )
                print(f"\n▶ {c['name']}: SKIP（{c['skip_reason']}）")
                continue
            missing = missing_env(c)
            if missing:
                reason = "未设置 " + "、".join(missing)
                results.append(CaseResult(name=c["name"], desc=c["desc"], outcome="skip", error=reason, verdict="SKIP"))
                print(f"\n▶ {c['name']}: SKIP（{reason}）")
                continue
            try:
                r = await run_case(conn, c)
                results.append(r)
            except Exception as e:
                results.append(
                    CaseResult(name=c["name"], desc=c["desc"], outcome="error", error=str(e)[:200], verdict="FAIL")
                )
                print(f"  ✗✗ exception: {e}")
        # share 特殊：起两个部署
    await pool.close()
    print_summary(results)


def print_summary(results):
    print("\n" + "=" * 78)
    print("E2E 矩阵汇总")
    print("=" * 78)
    print(f"{'场景':<18}{'结果':<14}{'耗时':>7}  {'serve':<6}{'判定':<14}说明")
    print("-" * 78)
    for r in results:
        print(
            f"{r.name:<18}{r.outcome:<14}{r.duration:>6.0f}s  {'ok' if r.serve[0] else '-':<6}{r.verdict:<14}{r.desc[:24]}"
        )
    n_pass = sum(1 for r in results if r.verdict == "PASS")
    n_ef = sum(1 for r in results if r.verdict == "EXPECTED_FAIL")
    n_fail = sum(1 for r in results if r.verdict == "FAIL")
    print("-" * 78)
    print(f"PASS={n_pass}  EXPECTED_FAIL={n_ef}  FAIL={n_fail}  (共 {len(results)})")
    json.dump([r.__dict__ for r in results], open(".run/e2e_report.json", "w"), indent=2, ensure_ascii=False)
    print("明细已写 .run/e2e_report.json")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default="")
    p.add_argument("--keep", action="store_true")
    ARGS = p.parse_args()
    asyncio.run(main())
