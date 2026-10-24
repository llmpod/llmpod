"""推理镜像映射 catalog（纯函数，零 DB）。

按 (engine, arch, gpu_model, driver_version, engine_version) 五维匹配，返回可用镜像**列表** +
额外参数（params）。数据在 ``llmpod/config/inference_images.json``（配置文件，零依赖）。

id 唯一标识一条镜像变体（同 engine_version 可多条——这正是返回 list 的原因）。
resolver 由部署链路（按 engine 选 entry 交适配器）与列举端点共用。
"""

from __future__ import annotations

import fnmatch
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from llmpod.core.exceptions import LLMPodValidationError

log = logging.getLogger("llmpod.apps.manager.deployment.image_catalog")


class ImageEntry(BaseModel):
    """一条镜像变体：五维键 → image + 额外参数。"""

    id: str
    engine: str
    arch: str
    gpu_model: list[str] = Field(default_factory=lambda: ["*"], description="GPU 型号 glob 列表（fnmatch Gpu.name）")
    gpu_model_exclude: list[str] = Field(
        default_factory=list, description="不兼容 GPU 型号 glob 列表（fnmatch Gpu.name）"
    )
    gpu_model_required: bool = Field(False, description="GPU 型号未知时不匹配，避免选择依赖型号的兼容条目")
    driver: str | None = Field(None, description="驱动版本区间，如 >=570 / >=530,<570；None=不限")
    driver_required: bool = Field(False, description="驱动版本未知时不匹配，避免选择依赖驱动分支的兼容条目")
    engine_version: str | None = Field(None, description="引擎版本（对应部署 image_tag）")
    image: str
    default: bool = False
    params: dict = Field(default_factory=dict, description="额外参数 floor：{args:[...], env:{...}}")
    # 解压后镜像体积（GB，可选）：PULL_IMAGE 看门狗按此分桶（无标注回落保守默认），
    # 小镜像断供时更快失败而非空等长超时。运维按 registry 实际大小标注即可。
    size_gb: float | None = Field(None, description="镜像体积（GB，看门狗分桶用；未标注走默认超时）")
    notes: str = ""


class EngineVocab(BaseModel):
    """某引擎版本的参数词表（JSON ``engine_versions`` 段，键 = engine/engine_version）。

    引擎各版本增删/改名解析器与 flag 是常态（如 vLLM 0.27 移除 ``--task``、0.30 重构
    parser 注册表），词表放在数据文件里按版本收录实际可用的名字——适配器注入前查表，
    查无此名就降档，新版本收录零代码改 JSON。收录范围是平台注入会用到的名字，非全量
    registry；新版本收录时按该版源码核实增删。

    缺省值语义分两类：解析器名表缺省（None）= 未收录该字段，不做该项门控（按最新），
    空表 = 该版本明确一个解析器都不认（全降档）；布尔/档位字段缺省 = 最新词表姿态，
    旧版本要降档须显式写 false/旧档。
    """

    model_config = ConfigDict(extra="forbid")  # 词表字段拼错拒载（warn+skip），防 typo 静默落到缺省档

    # 注册在册的 --tool-call-parser 名（vLLM）；None=不门控（按最新）| 空表=一个都不认
    tool_parsers: list[str] | None = None
    # 注册在册的 --reasoning-parser 名（vLLM）；None=不门控 | 空表=无推理解析器（连 flag 都没有的版本同表）
    reasoning_parsers: list[str] | None = None
    # embedding/rerank 任务词表档：task（--task embed/classify，<0.11）| runner_convert（--runner pooling --convert，≥0.11）
    pooling_flags: Literal["task", "runner_convert"] = "runner_convert"
    # --tool-call-parser/--reasoning-parser 传 auto 是否被该版本 choices 接受（sglang 0.5.12 起）
    parser_auto: bool = True
    # SGLang --enable-metrics 是否存在（0.4.x 起；更早镜像无此 flag）
    metrics_flag: bool = True
    # 是否支持平台拼装的多机 rendezvous flags；旧 SGLang 使用 --nccl-init-addr，平台当前
    # 的 --dist-init-addr 自 0.4.6 确认存在。vLLM 0.12 起才支持平台使用的 elastic 语义。
    multinode: bool = True
    # 是否支持流水线并行 flag（SGLang --pp-size 0.4.7 起才有；0.4.2/0.4.6 连 pp 概念都没有，
    # 传了必炸 argparse。不支持时 pp>1 部署组装期硬拒）
    pp: bool = True


def _catalog_path() -> Path:
    # image_catalog.py 在 llmpod/apps/manager/deployment/，config/ 在 llmpod/config/
    import llmpod.config as cfg

    return Path(cfg.__file__).parent / "inference_images.json"


@lru_cache(maxsize=1)
def _load() -> tuple[ImageEntry, ...]:
    """启动加载 JSON（模块级缓存）；文件缺 → 空列表 + warn；同 id 后者覆盖前者 + warn。"""
    p = _catalog_path()
    if not p.exists():
        log.warning("inference_images.json not found at %s; image catalog empty", p)
        return ()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("inference_images.json parse failed (%s); catalog empty", e)
        return ()
    seen: dict[str, ImageEntry] = {}
    for item in raw.get("images", []):
        try:
            entry = ImageEntry(**item)
        except Exception as e:
            log.warning("bad image entry %s skipped: %s", item.get("id", "?"), e)
            continue
        if entry.id in seen:
            log.warning("duplicate image id %s; latter overrides", entry.id)
        seen[entry.id] = entry
    return tuple(seen.values())


@lru_cache(maxsize=1)
def _load_vocabs() -> dict[tuple[str, str], EngineVocab]:
    """加载 ``engine_versions`` 段（键 "engine:version" → 词表）。文件缺/段缺 → 空表。"""
    p = _catalog_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("inference_images.json parse failed (%s); engine vocabs empty", e)
        return {}
    out: dict[tuple[str, str], EngineVocab] = {}
    for key, item in (raw.get("engine_versions") or {}).items():
        try:
            engine, _, version = str(key).partition(":")
            vocab = EngineVocab(**item)
        except Exception as e:
            log.warning("bad engine_versions entry %s skipped: %s", key, e)
            continue
        out[(engine, version)] = vocab
    return out


def reload() -> None:
    """清缓存重载（测试/热加载用）。"""
    _load.cache_clear()
    _load_vocabs.cache_clear()


def engine_vocab(engine: str, engine_version: str | None) -> EngineVocab | None:
    """某引擎版本的参数词表；版本未知（None/空）或未收录 → None（调用方按最新词表）。

    词表是文件不是代码：新引擎版本增删/改名解析器，改 ``engine_versions`` 段即可，
    不用动适配器。返回 None 与「收录了空词表」语义不同——前者是没数据（不降级），
    后者是明确「该版本什么解析器都不认」（全降档）。
    """
    if not engine_version:
        return None
    return _load_vocabs().get((engine, str(engine_version)))


def _ver_key(v: str) -> tuple[int, ...]:
    """版本号 → 可比较整数元组：'595.84'→(595,84)，'25.2.3'→(25,2,3)，'570'→(570,)。"""
    parts: list[int] = []
    for tok in str(v).replace("rc", ".").split("."):
        digits = "".join(ch for ch in tok if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _driver_ok(constraint: str | None, driver_version: str | None) -> bool:
    """driver 区间约束是否满足。constraint None → 放行；driver_version None → 放行（缺数据不卡）。"""
    if not constraint:
        return True
    if not driver_version:
        return True  # 节点未上报驱动 → 不按 driver 过滤（保守放行，由具体性排序兜底）
    dv = _ver_key(driver_version)
    for clause in str(constraint).split(","):
        clause = clause.strip()
        op = ""
        for cand in (">=", "<=", "==", "!=", ">", "<", "="):
            if clause.startswith(cand):
                op = cand
                break
        target = _ver_key(clause[len(op) :].strip())
        if op == ">=" and not dv >= target:
            return False
        if op == ">" and not dv > target:
            return False
        if op == "<=" and not dv <= target:
            return False
        if op == "<" and not dv < target:
            return False
        if op in ("=", "==") and not dv == target:
            return False
        if op == "!=" and not dv != target:
            return False
    return True


def _gpu_match(patterns: list[str], gpu_model: str | None) -> tuple[bool, bool]:
    """返回 (命中, 具体命中)。gpu_model None → (True, False) 放行不具体。"""
    if gpu_model is None:
        return True, False
    for pat in patterns or ["*"]:
        if fnmatch.fnmatchcase(gpu_model, pat):
            return True, pat != "*"  # 非全 * 的 pattern 命中视为「具体」
    return False, False


def _gpu_excluded(patterns: list[str], gpu_model: str | None) -> bool:
    """GPU 型号命中显式排除表；型号未知时不猜测。"""
    model = gpu_model.casefold() if gpu_model else ""
    return bool(model and any(fnmatch.fnmatchcase(model, pat.casefold()) for pat in patterns))


def _specificity(entry: ImageEntry, gpu_specific: bool, driver_known: bool) -> tuple[int, int, int]:
    """排序键（高优先）：gpu 具体命中 > driver 有约束 > default。

    driver 有约束仅当 ``driver_known``（节点已上报驱动）时构成「更具体」——驱动未知时
    约束未参与过滤（``_driver_ok`` 保守放行），若仍给 boost，会越过 default 误选到
    只适配特定驱动区间的旧镜像（如 cu121），并与 PULL/START 两阶段镜像口径（均走 catalog）
    漂移。故驱动未知时 driver-constrained 条目不越过 default。
    """
    driver_boost = 1 if (entry.driver and driver_known) else 0
    return (1 if gpu_specific else 0, driver_boost, 1 if entry.default else 0)


def resolve_images(
    *,
    engine: str,
    arch: str,
    gpu_model: str | None = None,
    driver_version: str | None = None,
    engine_version: str | None = None,
) -> list[ImageEntry]:
    """五维过滤 + specificity 排序，返回可用镜像列表（top 为首选）。"""
    matched: list[tuple[tuple[int, int, int], ImageEntry]] = []
    for e in _load():
        if e.engine != engine or e.arch != arch:
            continue
        if e.gpu_model_required and not gpu_model:
            continue
        if e.driver_required and not driver_version:
            continue
        ok, gpu_specific = _gpu_match(e.gpu_model, gpu_model)
        if not ok:
            continue
        if _gpu_excluded(e.gpu_model_exclude, gpu_model):
            continue
        if not _driver_ok(e.driver, driver_version):
            continue
        if engine_version is not None and e.engine_version != engine_version:
            continue
        matched.append((_specificity(e, gpu_specific, driver_version is not None), e))
    # sort 稳定性保证同分条目保持 catalog 声明顺序
    matched.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in matched]


def pick_entry(
    *,
    engine: str,
    arch: str,
    gpu_model: str | None = None,
    driver_version: str | None = None,
    engine_version: str | None = None,
) -> ImageEntry | None:
    """resolve_images 的 top 命中（无 → None）。"""
    items = resolve_images(
        engine=engine, arch=arch, gpu_model=gpu_model, driver_version=driver_version, engine_version=engine_version
    )
    return items[0] if items else None


def pick_image(
    *,
    engine: str,
    arch: str,
    gpu_model: str | None = None,
    driver_version: str | None = None,
    engine_version: str | None = None,
) -> str | None:
    """top 命中的 image ref（无 → None）。"""
    e = pick_entry(
        engine=engine, arch=arch, gpu_model=gpu_model, driver_version=driver_version, engine_version=engine_version
    )
    return e.image if e else None


def _sort_groups_by_version(entries: list[ImageEntry]) -> list[ImageEntry]:
    """按 (engine, arch) 分组、组内引擎版本从新到旧；无版本条目排在组尾。

    前端镜像版本下拉按本条列表的顺序首次出现去重展示，组内降序才能让最新版本排在最前。
    同版本的多驱动/多卡变体保持 JSON 声明顺序。
    """
    groups: dict[tuple[str, str], list[ImageEntry]] = {}
    for entry in entries:
        groups.setdefault((entry.engine, entry.arch), []).append(entry)

    ordered: list[ImageEntry] = []
    for group in groups.values():
        versioned: list[tuple[tuple[int, ...], ImageEntry]] = []
        unversioned: list[ImageEntry] = []
        for entry in group:
            if entry.engine_version:
                versioned.append((_ver_key(entry.engine_version), entry))
            else:
                unversioned.append(entry)
        versioned.sort(key=lambda pair: pair[0], reverse=True)
        ordered.extend(entry for _, entry in versioned)
        ordered.extend(unversioned)
    return ordered


def list_entries(
    *,
    engine: str | None = None,
    arch: str | None = None,
    engine_version: str | None = None,
    gpu_model: str | None = None,
    driver_version: str | None = None,
) -> list[ImageEntry]:
    """列举（端点用）：按给定维度过滤，组内引擎版本从新到旧（无版本条目置组尾）。"""
    out: list[ImageEntry] = []
    for e in _load():
        if engine is not None and e.engine != engine:
            continue
        if arch is not None and e.arch != arch:
            continue
        if e.gpu_model_required and not gpu_model:
            continue
        if e.driver_required and not driver_version:
            continue
        if engine_version is not None and e.engine_version != engine_version:
            continue
        ok, _ = _gpu_match(e.gpu_model, gpu_model)
        if not ok:
            continue
        if _gpu_excluded(e.gpu_model_exclude, gpu_model):
            continue
        if not _driver_ok(e.driver, driver_version):
            continue
        out.append(e)
    return _sort_groups_by_version(out)


def resolve_image_ref(
    *,
    engine: str,
    arch: str,
    gpu_model: str | None = None,
    driver_version: str | None = None,
    engine_version: str | None = None,
    explicit_image: str | None = None,
) -> tuple[str, ImageEntry | None]:
    """解析镜像 + catalog 条目：显式 image 优先，否则 catalog 命中；均无 → raise。

    返回 ``(image_ref, entry)``——``entry`` 供 ``assemble_args/assemble_env`` 的 image 层
    参数用；显式 image 覆盖时 catalog 仍尽力取 params（miss 则 entry=None，不 raise）。
    """
    entry = pick_entry(
        engine=engine,
        arch=arch,
        gpu_model=gpu_model,
        driver_version=driver_version,
        engine_version=engine_version,
    )
    image_ref = explicit_image or (entry.image if entry else None)
    if image_ref is None:
        raise LLMPodValidationError(f"推理引擎镜像暂无 {engine}/{arch} 支持性")
    return image_ref, entry


def explain_unresolved(
    *,
    engine: str,
    arch: str,
    gpu_model: str | None = None,
    driver_version: str | None = None,
    engine_version: str | None = None,
) -> str:
    """``resolve_image_ref`` 未命中时的人读原因，供 inspect 降级提示（非阻断）。

    放宽 driver/gpu 过滤，按 engine+arch+engine_version 找候选条目，列出**真实镜像**及其驱动/型号
    要求并标注当前卡是否满足——让用户知道「该升到什么驱动」或「该换什么 image_tag」。
    """
    cands = [
        e
        for e in _load()
        if e.engine == engine and e.arch == arch and (engine_version is None or e.engine_version == engine_version)
    ]
    tag = f"@{engine_version}" if engine_version else ""
    if not cands:
        return f"未找到 {engine}/{arch}{tag} 的镜像，请确认 image_tag 是否正确"

    def _fmt_reqs(e: ImageEntry) -> str:
        parts: list[str] = []
        if e.driver:
            ok = _driver_ok(e.driver, driver_version)
            parts.append(f"驱动 {e.driver}" + ("" if ok else f"（当前 {driver_version or '未知'} 不满足）"))
        gpu_ok, _ = _gpu_match(e.gpu_model, gpu_model)
        if e.gpu_model and e.gpu_model != ["*"]:
            models = "/".join(p.strip("*") for p in e.gpu_model)
            parts.append(f"型号 {models}" + ("" if gpu_ok else "（不匹配）"))
        if _gpu_excluded(e.gpu_model_exclude, gpu_model):
            parts.append(f"型号 {gpu_model} 不在兼容范围")
        return "、".join(parts) if parts else "无特殊约束"

    if len(cands) == 1:
        e = cands[0]
        return f"镜像 {e.image} 需要{_fmt_reqs(e)}；请升级驱动或换 image_tag"

    lines = [f"未找到兼容镜像（{engine}/{arch}{tag}），候选镜像要求："]
    for e in cands:
        lines.append(f"  • {e.image}：{_fmt_reqs(e)}")
    lines.append("请升级驱动、更换所选 GPU 或换 image_tag")
    return "\n".join(lines)
