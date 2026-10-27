"""bootstrap_params 引导参数模型单测（纯逻辑，无 HTTP）。"""

from __future__ import annotations

import re

import pytest

import llmpod
from llmpod.apps.webserver.utils.bootstrap_params import (
    DEFAULT_CONTAINER_NAME,
    DEFAULT_DATA_VOLUME,
    DEFAULT_IMAGE,
    BootstrapNodeParams,
    _SAFE_RE,
)
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.services.version_service import OFFICIAL_IMAGE


def _from_raw(**kw) -> BootstrapNodeParams:
    return BootstrapNodeParams.from_raw(**kw)


# ===== 默认值与填充 =====


def test_defaults_when_all_none():
    p = _from_raw()
    assert p.container_name == DEFAULT_CONTAINER_NAME == "llmpod-node"
    assert p.data_volume == DEFAULT_DATA_VOLUME == "llmpod-node-data"
    # 默认镜像跟随 manager 当前版本（官方镜像:<__version__>，与 manager 同代）
    assert p.image == DEFAULT_IMAGE == f"{OFFICIAL_IMAGE}:{llmpod.__version__}"
    assert p.node_ip == ""
    assert p.extra_mounts == []
    # 全默认 → 无 query 增量
    assert p.query_items("/data/models") == []


def test_empty_string_means_unset():
    """query 形态下 ?container_name= 空串应等同未传（走默认），而非报错。"""
    p = _from_raw(container_name="", data_volume="", image="")
    assert p.container_name == DEFAULT_CONTAINER_NAME
    assert p.data_volume == DEFAULT_DATA_VOLUME
    assert p.image == DEFAULT_IMAGE


def test_resolved_fills_model_dirs():
    p = _from_raw().resolved("/data/models")
    # canonical 容器内路径恒 = manager 配置（非用户参数）
    assert p.model_dir == "/data/models"
    assert p.model_host_dir == "/data/models"
    # 宿主目录与 canonical 不同 → 各自生效（大盘在别处）
    p2 = _from_raw(model_host_dir="/mnt/bigdisk/models").resolved("/data/models")
    assert p2.model_dir == "/data/models"
    assert p2.model_host_dir == "/mnt/bigdisk/models"


def test_resolved_revalidates_manager_default():
    """manager 配置的 model_store_dir 不经 from_raw 入口，resolved 时也要过白名单。"""
    with pytest.raises(LLMPodValidationError):
        _from_raw().resolved("/data models with space")


# ===== 校验矩阵 =====


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-ip",
        "$(id)",
        "1.2.3",
        "a b",
        "10.0.0.5;rm",
        "0.0.0.0",
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "fd00::1",
        "240.0.0.1",  # 保留段：与 node_addr/脚本 valid_ipv4 同口径剔除
        "255.255.255.255",  # 广播
        "10.0.0.08",  # 前导零段
    ],
)
def test_bad_node_ip_rejected(bad):
    with pytest.raises(LLMPodValidationError):
        _from_raw(node_ip=bad)


@pytest.mark.parametrize("good", ["10.0.0.5", "192.168.1.1"])
def test_good_node_ip_accepted(good):
    assert _from_raw(node_ip=good).node_ip == good


@pytest.mark.parametrize("bad", ["-x", "a b", "a;rm", "a`id`", "a$(id)", "llmpod/node"])
def test_bad_container_name_rejected(bad):
    with pytest.raises(LLMPodValidationError):
        _from_raw(container_name=bad)


def test_data_volume_both_forms_accepted():
    assert _from_raw(data_volume="llmpod-node-data2").data_volume == "llmpod-node-data2"
    assert _from_raw(data_volume="/mnt/big/models").data_volume == "/mnt/big/models"


@pytest.mark.parametrize("bad", ["../x", "a b", "a;b"])
def test_bad_data_volume_rejected(bad):
    with pytest.raises(LLMPodValidationError):
        _from_raw(data_volume=bad)


@pytest.mark.parametrize("bad", ["/a/../../etc", "/", "/data/../etc"])
def test_data_volume_traversal_forms_rejected(bad):
    """宿主 bind 路径拒 ``..`` 段与裸 ``/``（不该把宿主根目录挂进带 docker.sock 的容器）。"""
    with pytest.raises(LLMPodValidationError):
        _from_raw(data_volume=bad)


@pytest.mark.parametrize("bad", ["/a/../../etc:/b", "/:/host-root", "/etc/../shadows:/x"])
def test_bad_mount_src_traversal_rejected(bad):
    with pytest.raises(LLMPodValidationError):
        _from_raw(extra_mounts=[bad])


@pytest.mark.parametrize("bad", ["relative/path", "/a/../b", "/a/..", "/a b"])
def test_bad_model_host_dir_rejected(bad):
    with pytest.raises(LLMPodValidationError):
        _from_raw(model_host_dir=bad)


def test_good_mount_specs_accepted():
    """src=宿主绝对路径或卷名，dst=容器内绝对路径，mode 可带逗号。"""
    p = _from_raw(extra_mounts=["/mnt/hf:/hf:ro", "my-vol:/v", "/a:/b:rw,z"])
    assert p.extra_mounts == ["/mnt/hf:/hf:ro", "my-vol:/v", "/a:/b:rw,z"]


@pytest.mark.parametrize(
    "bad",
    [
        "/abs:rel",  # dst 必须容器内绝对路径
        "/a:/b;touch",  # 注入字符
        "/a:/b:rw,z:extra",  # mode 段非法
        "a b:/c",  # 空白
        ":/b",  # src 为空
        "",  # 空串（query 空串在路由层已被 from_raw 过滤，此处直传必拒）
    ],
)
def test_bad_mount_specs_rejected(bad):
    with pytest.raises(LLMPodValidationError):
        BootstrapNodeParams(
            extra_mounts=[bad],
        ).validate_all()


def test_duplicate_mount_rejected():
    with pytest.raises(LLMPodValidationError):
        _from_raw(extra_mounts=["/a:/b", "/a:/b"])


def test_too_many_mounts_rejected():
    with pytest.raises(LLMPodValidationError):
        _from_raw(extra_mounts=[f"/s{i}:/d{i}" for i in range(17)])


def test_image_forms():
    assert _from_raw(image="llmpod:latest").image == "llmpod:latest"
    assert _from_raw(image="reg.example:5000/llmpod/node:v1.2@sha256:" + "a" * 64).image.startswith("reg.example:5000/")
    with pytest.raises(LLMPodValidationError):
        _from_raw(image="bad image")


def test_image_plus_rejected():
    """镜像 ref 不收 ``+``：+ 进 URL 会被 query 解码成空格（表单编码歧义）。"""
    with pytest.raises(LLMPodValidationError):
        _from_raw(image="repo/img:1.0+build2")


# ===== query_items 组装 =====


def test_query_items_all_non_default_included():
    p = _from_raw(
        node_ip="10.0.0.5",
        container_name="gpu2-node",
        data_volume="/mnt/big/data",
        model_host_dir="/mnt/bigdisk/models",
        extra_mounts=["/mnt/hf:/hf:ro"],
        image="reg:5000/llmpod:v2",
    )
    items = p.query_items("/data/models")
    assert items == [
        ("node_ip", "10.0.0.5"),
        ("container_name", "gpu2-node"),
        ("data_volume", "/mnt/big/data"),
        ("model_host_dir", "/mnt/bigdisk/models"),
        ("extra_mounts", "/mnt/hf:/hf:ro"),
        ("image", "reg:5000/llmpod:v2"),
    ]


def test_query_items_model_dir_never_emitted():
    """model_dir 非用户参数（canonical 恒取 manager 配置），任何情况下不进 URL。"""
    p = _from_raw(model_host_dir="/mnt/bigdisk/models").resolved("/data/models")
    keys = [k for k, _ in p.query_items("/data/models")]
    assert "model_dir" not in keys


def test_query_items_host_dir_equal_model_dir_omitted():
    p = _from_raw(model_host_dir="/data/models")
    assert ("model_host_dir", "/data/models") not in p.query_items("/data/models")


def test_query_items_mounts_repeatable_pairs():
    p = _from_raw(extra_mounts=["/a:/b", "/c:/d:ro"])
    pairs = [k for k, _ in p.query_items("/data/models")]
    assert pairs == ["extra_mounts", "extra_mounts"]


# ===== render_items 渲染 =====


def test_render_items_join_and_safe_charset():
    p = _from_raw(extra_mounts=["/mnt/hf:/hf:ro", "/c:/d:rw,z"]).resolved("/data/models")
    rendered = p.render_items()
    assert rendered["extra_mounts"] == "/mnt/hf:/hf:ro /c:/d:rw,z"
    assert rendered["model_dir"] == "/data/models"
    # 每个渲染值必须过白名单（嵌 bash 脚本的注入兜底）；join 后的空格分隔符除外
    for key, value in rendered.items():
        if key == "extra_mounts":
            assert all(re.fullmatch(_SAFE_RE, m) for m in value.split(" ")), value
        else:
            assert re.fullmatch(_SAFE_RE, value), f"{key} 未过白名单: {value!r}"
