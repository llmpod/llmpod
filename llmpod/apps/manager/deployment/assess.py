"""逐卡部署匹配评估（纯函数，零 DB）。

给 inspect 的 cluster_gpus 每张卡附灰化语义 + 是否单卡可承载 + 所属选择组 + 所需显存参考值。
与 feasibility / estimate_required_vram 同源，不复制常量。

职责边界（与前端分层）：本模块只给单卡的**不变事实**——架构是否被引擎支持、是否在线、
所属组（同 cluster+同 arch+同 total_vram）、单卡(tp=1)是否充分。**tp 相关的判定不在本模块**：
「该卡在选定 tp 下显存够不够」「与已选组是否同显存」由前端按 required_vram_gb / tp 与
group_key 结构计算（用户选 tp/选组时实时变），故本模块不发 vram_insufficient 灰。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from llmpod.apps.manager.deployment import image_catalog
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.engine.base import estimate_required_vram
from llmpod.apps.manager.deployment.scheduler.gpu import card_reserve, reserve_fits
from llmpod.config.constants import GpuStatus
from llmpod.config.enums import ModelType


@dataclass
class GpuAssessment:
    """单卡匹配评估结果。"""

    uuid: str
    grey_reason: str | None = None  # "arch_mismatch" | "no_image" | "offline" | None(=可选用)
    grey_detail: str | None = None  # 灰化原因人读详情（no_image 时必填，前端 tooltip 用）；可选用为 None
    fit: bool = False  # 单卡(tp=1)即可承载：整卡默认预留装得下所需、且实时可用装得下预留；size 未知乐观 True
    group_key: str | None = None  # f"{cluster_id}:{arch}:{total_vram_gb}"；离线卡为 None
    required_vram_gb: float | None = None  # 模型整体所需(size+KV)；size 未知为 None


def assess_per_gpu(
    gpus,
    *,
    engine: str | None,
    model_size_gb: float | None,
    context_size: int | None,
    cluster_id: int | None = None,
    engine_version: str | None = None,
    explicit_image: str | None = None,
    model_type: str | None = None,
    kv_dtype_bytes: float | None = None,
    arch_meta=None,
    n_parallel: int | None = None,
) -> list[GpuAssessment]:
    """逐卡评估。

    入参 ``gpus`` 为任意带下列属性的对象列表：``uuid / node_id / cluster_id(可选) /
    index / arch / total_vram_gb / available_vram_gb / status``；镜像判定还会读
    ``name``（catalog gpu_model glob 匹配）与 ``driver_version``（catalog 驱动区间），缺省 None。

    - 架构判定：``engine`` 的 ``supported_gpu_archs`` 不含该卡 arch → grey=arch_mismatch；
      engine 未知 → 不做架构判定。
    - 镜像判定：架构通过后，按 (engine, 该卡 arch, 卡型号 name, driver_version, engine_version)
      查 catalog ``pick_entry``；无命中（型号不在任何 glob / 驱动不满足 / image_tag 无条目）→
      grey=no_image + grey_detail 带人读原因。``explicit_image`` 非空则跳过（escape-hatch 覆盖
      catalog，不灰）。engine 未知 → 跳过。
    - 显存参考：``required_vram_gb = estimate_required_vram(size, ctx)``（含 KV，复用 base）；
      ``model_type`` 为 embedding/rerank 时 KV=0（类别无自回归 KV cache）；size 未知 → None，
      跳过单卡 fit 判定（乐观 fit=True）。KV 精度按 ``kv_dtype_bytes``（或 ``arch_meta`` 真公式）折算。
    - 放行阈值与调度同源（``card_reserve`` + ``reserve_fits``：整卡默认预留装得下模型，
      且卡的实时可用装得下预留——被占用的卡 fit=False 但不灰，组内可凑 TP）。
    """
    supported_archs: set[str] | None = None
    if engine:
        try:
            supported_archs = set(get_adapter(engine).supported_gpu_archs)
        except Exception:
            supported_archs = None

    # embedding/rerank 类别无自回归 KV cache（类别级判定，与引擎无关）
    required = estimate_required_vram(
        model_size_gb,
        context_size,
        has_kv_cache=ModelType.has_kv_cache(model_type),
        dtype_bytes=kv_dtype_bytes,
        arch_meta=arch_meta,
        n_parallel=n_parallel,
    )  # size 未知 → None

    out: list[GpuAssessment] = []
    for g in gpus:
        cid = getattr(g, "cluster_id", None)
        if cid is None:
            cid = cluster_id
        status = getattr(g, "status", GpuStatus.ONLINE) or GpuStatus.ONLINE
        arch = getattr(g, "arch", "") or ""
        total = getattr(g, "total_vram_gb", 0.0) or 0.0
        avail = getattr(g, "available_vram_gb", 0.0) or 0.0
        uuid = getattr(g, "uuid", "")

        if status != GpuStatus.ONLINE:
            out.append(GpuAssessment(uuid=uuid, grey_reason="offline", group_key=None, required_vram_gb=required))
            continue

        group_key = f"{cid}:{arch}:{total}"

        if supported_archs is not None and arch not in supported_archs:
            out.append(
                GpuAssessment(uuid=uuid, grey_reason="arch_mismatch", group_key=group_key, required_vram_gb=required)
            )
            continue

        # 镜像可用性：架构通过后按卡（型号 + 驱动）查 catalog；无命中 → no_image 灰掉。
        # explicit_image 是用户显式 escape-hatch，覆盖 catalog，不灰。
        if engine and not explicit_image:
            gpu_model = getattr(g, "name", None)
            driver_version = getattr(g, "driver_version", None)
            entry = image_catalog.pick_entry(
                engine=engine,
                arch=arch,
                gpu_model=gpu_model,
                driver_version=driver_version,
                engine_version=engine_version,
            )
            if entry is None:
                out.append(
                    GpuAssessment(
                        uuid=uuid,
                        grey_reason="no_image",
                        grey_detail=image_catalog.explain_unresolved(
                            engine=engine,
                            arch=arch,
                            gpu_model=gpu_model,
                            driver_version=driver_version,
                            engine_version=engine_version,
                        ),
                        fit=False,
                        group_key=group_key,
                        required_vram_gb=required,
                    )
                )
                continue

        # 单卡(tp=1)是否充分：与调度同口径（card_reserve + reserve_fits）——整卡预留
        # （默认占比×total）既装得下模型，卡的**实时可用**也装得下这份预留。被占用超过
        # 一成的卡不再标「单卡可用」（推荐按实时可用判，不按总量判）；size 未知乐观放行
        # （前端按 required/tp 再判）
        fit = True if required is None else reserve_fits(card_reserve(g, None), required, avail)
        out.append(
            GpuAssessment(
                uuid=uuid, grey_reason=None, grey_detail=None, fit=fit, group_key=group_key, required_vram_gb=required
            )
        )
    return out


def archs_with_image(
    gpus,
    *,
    engine: str | None,
    engine_version: str | None = None,
    explicit_image: str | None = None,
) -> set[str]:
    """池内「有可用卡」的架构集合：在线 + 架构被引擎支持 + 有匹配镜像。

    调度回落序（``EngineAdapter.schedulable_archs``）据此收窄：没有镜像的架构留着只会让
    调度把副本落上去、等容器阶段才报「镜像暂无」。判定直接走 :func:`assess_per_gpu`
    （逐 arch 一次批量调用），不另写一份 catalog 键与在线/架构闸门——两份判定迟早漂移，
    而漂移的后果是回落序里多一个落不上的架构（静默）。
    """
    by_arch: dict[str, list] = defaultdict(list)
    for g in gpus:
        by_arch[getattr(g, "arch", "") or ""].append(g)
    out: set[str] = set()
    for arch, cards in by_arch.items():
        assessments = assess_per_gpu(
            cards,
            engine=engine,
            model_size_gb=None,  # 只取架构+镜像判定；显存参考值这里不用（required=None → fit 乐观）
            context_size=None,
            engine_version=engine_version,
            explicit_image=explicit_image,
        )
        if any(a.grey_reason is None for a in assessments):
            out.add(arch)
    return out


def check_protected_overlap(engine: str, extra_args: list[str] | None) -> list[str]:
    """返回 extra_args 中与引擎 protected_flags 重叠的 flag（供告警，不拒）。

    合并器会按 flag 名字维度静默跳过这些 flag（系统必控不被覆盖，``--flag=value``
    连写与 ``--flag value`` 同名同跳）；本函数用同一归一口径把「用户传了但被忽略」
    的情况显式列出，供 create 记日志 / 前端提示。引擎未知或无 extra_args → 空。
    """
    if not extra_args:
        return []
    try:
        protected = set(get_adapter(engine).protected_flags)
    except Exception:
        return []
    if not protected:
        return []
    from llmpod.apps.manager.deployment.engine.base import _flag_name

    return [a for a in extra_args if _flag_name(str(a)) in protected]
