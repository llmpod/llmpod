"""完整启动命令模板：占位符校验 / 渲染 / shell 判定 / 适配器拆分。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.command import (
    COMMAND_PLACEHOLDER_HINTS,
    command_argv,
    command_needs_shell,
    command_placeholder_names,
    join_command,
    render_command_template,
    validate_command_for_topology,
    validate_command_template,
)
from llmpod.apps.manager.deployment.engine import get_adapter, registered_engines
from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology
from llmpod.apps.manager.deployment.schemas.config import UnitTemplate
from llmpod.core.exceptions import LLMPodValidationError

# 占位符契约的权威快照：新增/删除占位符必须同时改这里（对账单测）
EXPECTED_PLACEHOLDERS = {
    "model_path",
    "served_model_name",
    "port",
    "tp",
    "pp",
    "nnodes",
    "node_rank",
    "master_addr",
    "master_port",
    "headless",
    "management_port",
    "metrics_port",
    "device_ids",
    "model_path_json",
    "served_model_name_json",
}


def _inst(**kw) -> InstanceSpec:
    base = {
        "model_path": "/data/models/Qwen3-8B",
        "served_model_name": "Qwen3-8B",
        "port": 8001,
        "tp": 2,
        "pp": 1,
        "nnodes": 2,
        "node_rank": 1,
        "master_addr": "10.0.0.5",
        "master_port": 29501,
    }
    base.update(kw)
    return InstanceSpec(**base)


class TestPlaceholderContract:
    """占位符表与各引擎回显映射必须一致；新增占位符时这里会先红。"""

    def test_placeholder_set_is_pinned(self):
        assert set(COMMAND_PLACEHOLDER_HINTS) == EXPECTED_PLACEHOLDERS

    def test_render_covers_every_registered_placeholder(self):
        command = " ".join("{" + name + "}" for name in EXPECTED_PLACEHOLDERS)
        rendered = render_command_template(command, _inst(), get_adapter("vllm"))
        assert "{" not in rendered

    def test_adapter_flag_maps_use_known_placeholders(self):
        for engine in registered_engines():
            adapter = get_adapter(engine)
            for placeholder in adapter.command_placeholder_flags.values():
                for name in command_placeholder_names(placeholder):
                    assert name in EXPECTED_PLACEHOLDERS, f"{engine}: {placeholder}"


class TestValidateCommandTemplate:
    def test_valid_single_node(self):
        validate_command_template("myvllm xxxx --port {port} --model {model_path}", has_multinode=False)

    def test_valid_multinode_placeholders(self):
        validate_command_template(
            "myvllm xxxx --port {port} --node-rank {node_rank} --master-addr {master_addr}",
            has_multinode=True,
        )

    def test_none_is_allowed(self):
        validate_command_template(None, has_multinode=False)

    def test_empty_rejected(self):
        with pytest.raises(LLMPodValidationError):
            validate_command_template("   ", has_multinode=False)

    def test_unknown_placeholder_rejected(self):
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_template("myvllm --port {port} --model {model_paht}", has_multinode=False)
        assert "model_paht" in str(exc.value)

    def test_port_required(self):
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_template("myvllm --model {model_path}", has_multinode=False)
        assert "{port}" in str(exc.value)

    def test_master_placeholder_requires_multinode(self):
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_template(
                "myvllm --port {port} --master-addr {master_addr}", has_multinode=False
            )
        assert "master_addr" in str(exc.value)

    def test_unbalanced_quotes_rejected(self):
        with pytest.raises(LLMPodValidationError):
            validate_command_template("myvllm --prompt 'abc --port {port}", has_multinode=False)

    def test_shell_variable_is_not_a_placeholder(self):
        # ${PORT} 是 shell 变量，不按平台占位符校验
        validate_command_template("myvllm --port {port} --x ${PORT}", has_multinode=False)
        assert command_placeholder_names("myvllm --x ${PORT} --port {port}") == ["port"]


class TestValidateCommandForTopology:
    def test_mixed_nnodes_rejected(self):
        topology = Topology(copies=[UnitTemplate(nnodes=1), UnitTemplate(nnodes=2)])
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_for_topology("myvllm --port {port}", topology, get_adapter("vllm"))
        assert "跨机节点数一致" in str(exc.value)

    def test_multinode_requires_headless(self):
        topology = Topology(copies=[UnitTemplate(nnodes=2)])
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_for_topology("myvllm --port {port}", topology, get_adapter("vllm"))
        assert "headless" in str(exc.value)

    def test_single_node_needs_no_headless(self):
        topology = Topology(copies=[UnitTemplate(nnodes=1)])
        validate_command_for_topology("myvllm --port {port}", topology, get_adapter("vllm"))

    def test_extra_args_rejected_when_engine_declares_conflict(self):
        """命令模式 + extra_args：引擎声明无法落地（MindIE 靠引导脚本 "$@" 透传）即拒。"""
        topology = Topology(copies=[UnitTemplate(nnodes=1)])
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_for_topology(
                "mycmd --port {port}", topology, get_adapter("mindie"), {"extra_args": ["--x"]}
            )
        assert "extra_args" in str(exc.value)

    def test_extra_args_allowed_without_command(self):
        """自动生成路径下 extra_args 照常由引导脚本透传，不拦。"""
        topology = Topology(copies=[UnitTemplate(nnodes=1)])
        validate_command_for_topology(None, topology, get_adapter("mindie"), {"extra_args": ["--x"]})

    def test_extra_args_allowed_when_engine_has_no_conflict(self):
        """未声明冲突的引擎维持原语义：命令接管，多余参数静默忽略。"""
        topology = Topology(copies=[UnitTemplate(nnodes=1)])
        validate_command_for_topology(
            "vllm serve {model_path} --port {port}", topology, get_adapter("vllm"), {"extra_args": ["--x"]}
        )


class TestRenderCommandTemplate:
    def test_all_placeholders(self):
        command = (
            "myvllm --model {model_path} --name {served_model_name} --port {port} "
            "--tp {tp} --pp {pp} --nnodes {nnodes} --rank {node_rank} "
            "--addr {master_addr} --mp {master_port}"
        )
        rendered = render_command_template(command, _inst(), get_adapter("vllm"))
        assert rendered == (
            "myvllm --model /data/models/Qwen3-8B --name Qwen3-8B --port 8001 "
            "--tp 2 --pp 1 --nnodes 2 --rank 1 --addr 10.0.0.5 --mp 29501"
        )

    def test_shell_variable_untouched(self):
        rendered = render_command_template("myvllm --port {port} --key ${PORT}", _inst(port=8123), get_adapter("vllm"))
        assert rendered == "myvllm --port 8123 --key ${PORT}"

    def test_embedded_placeholder(self):
        rendered = render_command_template("myvllm --addr={master_addr}:{master_port}", _inst(), get_adapter("vllm"))
        assert rendered == "myvllm --addr=10.0.0.5:29501"

    def test_path_with_space_is_quoted(self):
        rendered = render_command_template(
            "myvllm --model {model_path}", _inst(model_path="/data/my models/m"), get_adapter("vllm")
        )
        assert rendered == "myvllm --model '/data/my models/m'"
        assert command_argv(rendered) == ["myvllm", "--model", "/data/my models/m"]

    def test_user_quoted_placeholder_not_double_quoted(self):
        rendered = render_command_template(
            'myvllm --model "{model_path}"', _inst(model_path="/data/my models/m"), get_adapter("vllm")
        )
        assert rendered == 'myvllm --model "/data/my models/m"'

    def test_headless_placeholder_by_rank(self):
        command = "vllm serve --port {port} {headless}"
        head = render_command_template(command, _inst(node_rank=0, nnodes=2), get_adapter("vllm"))
        worker = render_command_template(command, _inst(node_rank=1, nnodes=2), get_adapter("vllm"))
        assert head == "vllm serve --port 8001 "
        assert worker == "vllm serve --port 8001 --headless"

    def test_quoted_empty_placeholder_removes_quotes(self):
        """用户把空值占位符用引号包住时，不能留下空参数 ""。"""
        rendered = render_command_template(
            'vllm serve --port {port} "{headless}"', _inst(node_rank=0, nnodes=2), get_adapter("vllm")
        )
        assert rendered == "vllm serve --port 8001 "
        assert command_argv(rendered) == ["vllm", "serve", "--port", "8001"]

    def test_engine_specific_placeholder_empty_for_other_engines(self):
        """sglang 没有 --headless；占位符要渲染成空串，不能把 {...} 漏给容器。"""
        rendered = render_command_template(
            "python3 -m sglang.launch_server --port {port} {headless}",
            _inst(node_rank=1),
            get_adapter("sglang"),
        )
        assert "{headless}" not in rendered
        assert command_argv(rendered) == ["python3", "-m", "sglang.launch_server", "--port", "8001"]


class TestRequiredPlaceholders:
    def test_missing_required_rejected(self):
        with pytest.raises(LLMPodValidationError) as exc:
            validate_command_template(
                "vllm serve --port {port}", has_multinode=True, required=["headless"]
            )
        assert "headless" in str(exc.value)

    def test_required_present(self):
        validate_command_template(
            "vllm serve --port {port} {headless}", has_multinode=True, required=["headless"]
        )


class TestCommandNeedsShell:
    def test_plain_command(self):
        assert command_needs_shell("myvllm xxx --port 8000 --model /data/m") is False

    def test_pipe(self):
        assert command_needs_shell("myvllm xxx --port 8000 | tee /tmp/x") is True

    def test_and(self):
        assert command_needs_shell("source /opt/env.sh && exec myvllm xxx --port 8000") is True

    def test_command_substitution(self):
        assert command_needs_shell("myvllm xxx --port $(echo 8000)") is True

    def test_shell_variable(self):
        assert command_needs_shell("myvllm xxx --port ${PORT}") is True

    def test_unspaced_redirection(self):
        assert command_needs_shell("myvllm xxx --port 8000 >/tmp/x") is True
        assert command_needs_shell("myvllm xxx --port 8000 2>&1") is True
        assert command_needs_shell("myvllm xxx --port 8000 2>/dev/null") is True
        assert command_needs_shell("myvllm xxx --port 8000 </dev/null") is True

    def test_unspaced_pipe(self):
        assert command_needs_shell("myvllm xxx --port 8000 |tee /tmp/x") is True

    def test_quoted_metachar_is_not_shell(self):
        assert command_needs_shell('myvllm xxx --url "a&b" --port 8000') is False


class TestJoinCommand:
    def test_placeholders_not_quoted(self):
        joined = join_command(["myvllm", "--model", "{model_path}", "--port", "{port}"])
        assert joined == "myvllm --model {model_path} --port {port}"

    def test_json_arg_quoted_and_roundtrips(self):
        arg = '{"cudagraph_mode": "FULL_DECODE_ONLY"}'
        joined = join_command(["vllm", "serve", "--compilation-config", arg])
        assert command_argv(joined) == ["vllm", "serve", "--compilation-config", arg]


class TestAdapterCommandSplit:
    def test_vllm_nvidia_keeps_image_entrypoint(self):
        adapter = get_adapter("vllm")
        entrypoint, args = adapter.split_command(["vllm", "serve", "--port", "8000"], "nvidia")
        assert entrypoint is None
        assert args == ["--port", "8000"]

    def test_vllm_cann_keeps_prefix_in_args(self):
        adapter = get_adapter("vllm")
        tokens = ["vllm", "serve", "/data/m", "--port", "8000"]
        entrypoint, args = adapter.split_command(tokens, "cann")
        assert entrypoint is None
        assert args == tokens

    def test_vllm_cann_custom_binary_replaces_entrypoint(self):
        """cann 镜像换成自定义程序时完全接管入口（CANN 环境初始化要自己写进命令）。"""
        adapter = get_adapter("vllm")
        tokens = ["myvllm", "xxxx", "--port", "8000"]
        entrypoint, args = adapter.split_command(tokens, "cann")
        assert entrypoint == tokens
        assert args == []

    def test_vllm_cann_shell_command_keeps_image_entrypoint(self):
        """cann + shell 语法：命令进 args，保住镜像 bash-source 入口（CANN 环境不丢）。"""
        adapter = get_adapter("vllm")
        entrypoint, args = adapter.command_run_argv("vllm serve /m --port 8000 | tee /tmp/x", "cann")
        assert entrypoint is None
        assert args == ["bash", "-c", "vllm serve /m --port 8000 | tee /tmp/x"]

    def test_vllm_nvidia_shell_command_runs_through_bash(self):
        """nvidia 无 bootstrap 入口，shell 命令照旧交给 /bin/bash -c。"""
        adapter = get_adapter("vllm")
        entrypoint, args = adapter.command_run_argv("vllm serve /m --port 8000 | tee /tmp/x", "nvidia")
        assert entrypoint == ["/bin/bash", "-c", "vllm serve /m --port 8000 | tee /tmp/x"]
        assert args == []

    def test_custom_binary_replaces_entrypoint(self):
        adapter = get_adapter("vllm")
        tokens = ["myvllm", "xxxx", "--port", "8000"]
        entrypoint, args = adapter.split_command(tokens, "nvidia")
        assert entrypoint == tokens
        assert args == []

    def test_sglang_strips_launcher(self):
        adapter = get_adapter("sglang")
        tokens = ["python3", "-m", "sglang.launch_server", "--port", "8000"]
        entrypoint, args = adapter.split_command(tokens, "nvidia")
        assert entrypoint == ["python3", "-m", "sglang.launch_server"]
        assert args == ["--port", "8000"]


class TestAdapterCommandTemplate:
    def test_vllm_template(self):
        adapter = get_adapter("vllm")
        inst = _inst(nnodes=1, node_rank=0, master_addr=None, master_port=None)
        tokens = ["vllm", "serve", "--model", "/data/models/Qwen3-8B", "--served-model-name", "Qwen3-8B", "--port", "8001"]
        template = adapter.command_template(tokens, inst)
        assert template == "vllm serve --model {model_path} --served-model-name {served_model_name} --port {port}"

    def test_vllm_cann_positional_model_path(self):
        adapter = get_adapter("vllm")
        inst = _inst(nnodes=1, node_rank=0, master_addr=None, master_port=None)
        tokens = ["vllm", "serve", "/data/models/Qwen3-8B", "--port", "8001"]
        template = adapter.command_template(tokens, inst)
        assert template == "vllm serve {model_path} --port {port}"

    def test_vllm_multinode_template_includes_headless(self):
        adapter = get_adapter("vllm")
        template = adapter.command_template(["vllm", "serve", "--port", "8000"], _inst(nnodes=2))
        assert template == "vllm serve --port {port} {headless}"

    def test_sglang_dist_init_addr(self):
        adapter = get_adapter("sglang")
        inst = _inst(port=8000)
        tokens = ["python3", "-m", "sglang.launch_server", "--port", "8000", "--dist-init-addr", "10.0.0.5:5001"]
        template = adapter.command_template(tokens, inst)
        assert template == (
            "python3 -m sglang.launch_server --port {port} "
            "--dist-init-addr {master_addr}:{master_port}"
        )


class TestCommandModeEnv:
    def test_catalog_image_env_skipped_in_command_mode(self):
        from types import SimpleNamespace

        from llmpod.apps.manager.deployment.schemas import EngineConfig, Topology

        adapter = get_adapter("vllm")
        entry = SimpleNamespace(params={"env": {"IMAGE_ONLY": "1"}})
        inst = _inst()
        with_image = adapter.assemble_env(inst, Topology(tp=1), EngineConfig(), entry, {})
        without_image = adapter.assemble_env(inst, Topology(tp=1), EngineConfig(), None, {})
        assert with_image["IMAGE_ONLY"] == "1"
        assert "IMAGE_ONLY" not in without_image


class TestMemoryUtilizationFromCommand:
    def test_command_flag_drives_ledger_value(self):
        adapter = get_adapter("vllm")
        assert adapter.memory_utilization_from({}, "vllm serve --gpu-memory-utilization 0.5 --port {port}") == 0.5

    def test_bad_command_falls_back_to_default(self):
        adapter = get_adapter("vllm")
        assert adapter.memory_utilization_from({}, "vllm serve --prompt 'unbalanced") == adapter.default_gpu_memory_utilization

    def test_extra_args_ignored_when_command_present(self):
        """命令模式下 extra_args 不执行，账本也不能按它记账。"""
        adapter = get_adapter("vllm")
        assert (
            adapter.memory_utilization_from(
                {"extra_args": ["--gpu-memory-utilization", "0.3"]},
                "vllm serve --port {port}",
            )
            == adapter.default_gpu_memory_utilization
        )
