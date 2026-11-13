"""节点容器一律带 ``--privileged``：引导脚本与 ``make node-up`` 两条启动路径。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "addnode_template.sh"


def test_privileged_flag_is_added_once_at_top_level():
    """``--privileged`` 只在顶层加一次，不嵌在任何按卡型分叉的分支里。

    这就是「CPU-only / NVIDIA / Ascend 全都给」的直接表达：三条路径共用同一行，
    没有哪类节点走不到。
    """
    lines = TEMPLATE.read_text().splitlines()
    hits = [index for index, line in enumerate(lines) if line.strip() == "RUN_FLAGS+=(--privileged)"]

    assert len(hits) == 1, "特权标志应恰好出现一次"
    index = hits[0]
    assert lines[index] == "RUN_FLAGS+=(--privileged)", "必须是顶格语句，不能被缩进到某个 if 分支里"


def test_docker_run_consumes_run_flags():
    """启动命令用 ``${RUN_FLAGS[@]}`` 展开，顶层加的标志因此一定进容器。"""
    assert '"${RUN_FLAGS[@]}"' in TEMPLATE.read_text()


def test_makefile_node_up_is_privileged():
    """``make node-up`` 是本地起节点的另一条路径，与引导脚本保持一致。"""
    makefile = (ROOT / "Makefile").read_text()
    recipe = makefile.split("node-up: .env\n", 1)[1].split("\nnode-build-up:", 1)[0]

    assert "--privileged" in recipe
