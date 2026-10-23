"""并行度形状约束：tp 能否切得开模型权重。

**为什么单列一处**：这套规则是引擎源码级的硬约束，不是启发式——不满足时容器照样起来，
引擎在初始化阶段才炸（有的还是裸 AssertionError），排障成本全落在用户侧。收敛在这里，
排查期、inspect、create、编排四层共用同一判据。

约束按引擎真实语义（vLLM 口径，见 ``config/model.py`` 与 ``layers/linear.py``）：

- ``num_attention_heads % tp == 0``：config 期硬抛，报错友好；
- KV head：``kv >= tp`` 时要求 ``kv % tp == 0``（按 head 切），``kv < tp`` 时要求
  ``tp % kv == 0``（vLLM 复制 KV head，每卡至少 1 个）；不满足在建层期 AssertionError；
- ``num_hidden_layers % pp`` **不要求**：vLLM 允许不均匀切层，只打一条 INFO；
- ``vocab_size % tp`` **不要求**：vLLM 把词表 pad 到 64 的倍数再切。

形状未知（老模型行没探测过）一律放行：宁可漏报也不误拒——把能跑的部署拦住比放过去更糟。

上面这套是 **vLLM 口径**；引擎不按 head 切分 tp（llama.cpp 走 layer 切分）时整套不适用，
由 :func:`shape_for` 返回空形状把校验摘掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from llmpod.config.enums import ModelType
from llmpod.core.exceptions import LLMPodValidationError

# 报错信息里提示的候选 tp 个数上限（列全了反而看不清）
_MAX_SUGGESTED_TP = 6


@dataclass(frozen=True)
class ParallelShape:
    """参与 tp 整除判定的模型侧形状。两字段都可缺（缺则该维度不参与判定）。"""

    num_attention_heads: int | None = None
    num_kv_heads: int | None = None

    @classmethod
    def from_model(cls, model) -> "ParallelShape":
        """从 Model 行（或任何带同名字段的对象）取形状；未探测过的字段为 None。"""
        return cls(
            num_attention_heads=_positive(getattr(model, "num_attention_heads", None)),
            num_kv_heads=_positive(getattr(model, "num_kv_heads", None)),
        )

    @property
    def known(self) -> bool:
        """是否有任一维度可用；全缺时所有校验短路放行。"""
        return bool(self.num_attention_heads or self.num_kv_heads)


def shape_for(adapter, model) -> ParallelShape:
    """该引擎下**有约束力**的模型 head 形状（``adapter``/``model`` 鸭子类型）。

    引擎不按 head 切分 tp（如 llama.cpp 走 layer 切分，多卡不要求 ``heads % tp == 0``）时返回
    **空形状**：空形状在本模块下游一律表示「不约束」——``known`` 为 False → tp 校验放行、
    ``suggested_tp_sizes`` 退化成 ``1..卡数``。因此预检 / 调度 / 推导三个使用点都只接这一个
    函数，不必各自判引擎语义（判漏一处的后果就是又把能跑的部署拦住）。
    """
    if not getattr(adapter, "tp_splits_by_heads", True):
        return ParallelShape()
    return ParallelShape.from_model(model)


def _positive(value) -> int | None:
    """只认正整数：0/负数/非整数（含 bool）一律当未知。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _kv_ok(kv: int, tp: int) -> bool:
    """KV head 与 tp 的匹配（vLLM ``QKVParallelLinear`` 的两条分支）。"""
    return kv % tp == 0 if kv >= tp else tp % kv == 0


def _tp_failure(shape: ParallelShape, tp: int) -> str | None:
    """tp 卡在哪个维度上：``"attention"`` / ``"kv"`` / ``"kv_too_few"``；合法返回 None。

    ``tp_ok`` 与 ``tp_reject_reason`` 的唯一判定来源——两处各写一遍取模，迟早会漂成
    「判合法」与「报原因」不一致。
    """
    if tp <= 1 or not shape.known:
        return None
    heads = shape.num_attention_heads
    if heads is not None and heads % tp != 0:
        return "attention"
    kv = shape.num_kv_heads
    if kv is None or _kv_ok(kv, tp):
        return None
    return "kv" if kv >= tp else "kv_too_few"


def tp_ok(shape: ParallelShape, tp: int) -> bool:
    """tp 能否切开该形状。tp<=1 或形状未知恒 True。"""
    return _tp_failure(shape, tp) is None


def suggested_tp_sizes(shape: ParallelShape, max_tp: int | None = None) -> list[int]:
    """合法 tp 升序列表（含 1）。``max_tp`` 给上限（如节点卡数，0 = 一张可用的都没有）；
    不给则按 head 数枚举。"""
    if max_tp is None:
        max_tp = max(shape.num_attention_heads or 0, shape.num_kv_heads or 0) or 1
    # 别把 max_tp 夹到 ≥1：0 张可用卡时「没有合法 tp」是正确答案，夹成 1 会让调用方
    # 拿到 [1] 再去索引不存在的卡（空候选越界）
    return [tp for tp in range(1, max_tp + 1) if tp_ok(shape, tp)]


def tp_reject_reason(shape: ParallelShape, tp: int) -> str | None:
    """tp 与模型形状不符时返回人读原因，合法返回 None。

    两个 head 约束都查：attention head 是引擎 config 期就拒的硬约束，KV head 是引擎自己
    都不提前查、要等建层才 AssertionError 的那条——补上它才是本函数的主要价值。
    """
    failure = _tp_failure(shape, tp)
    if failure is None:
        return None
    tail = f"可用 tp：{_format_sizes(suggested_tp_sizes(shape))}"
    heads = shape.num_attention_heads
    kv = shape.num_kv_heads
    if failure == "attention":
        return (
            f"attention head 数（{heads}）不能被 tp={tp} 整除：引擎按 head 切分权重，切不开会在引擎"
            f"初始化阶段直接失败；{tail}"
        )
    if failure == "kv":
        return (
            f"KV head 数（{kv}）不能被 tp={tp} 整除：引擎按 KV head 切分 KV 权重，切不开会在引擎"
            f"初始化阶段失败（该错误引擎不提前报，属最难定位的一类）；{tail}"
        )
    return (
        f"tp={tp} 不是 KV head 数（{kv}）的整数倍：KV head 少于 tp 时引擎按整数倍复制 KV head，"
        f"{tp}÷{kv} 除不尽无法均分；{tail}"
    )


def validate_parallel_shape(topology, shape: ParallelShape) -> None:
    """逐份校验 tp 能切开模型（编排器跳过路由直调 ``_plan`` 时的入口）。

    ``shape`` 未探测（空 ``ParallelShape``）→ 逐份放行。结构类约束，不受 ``force`` 影响：
    force 只放行容量超额那类「本来能跑、只是挤一挤」的判断，权重切不开是引擎层面必然失败，
    不该被 force 掩盖。
    """
    units = topology.per_copy_units()
    heterogeneous = not topology.is_homogeneous
    for seq, unit in enumerate(units):
        reason = tp_reject_reason(shape, max(1, int(unit.tp or 1)))
        if reason:
            tag = f"第 {seq + 1} 份：" if heterogeneous else ""
            raise LLMPodValidationError(f"{tag}{reason}")


def _format_sizes(sizes: list[int]) -> str:
    if not sizes:
        return "该形状下无合法 tp"
    shown = sizes[:_MAX_SUGGESTED_TP]
    text = "、".join(str(s) for s in shown)
    return f"{text} 等" if len(sizes) > len(shown) else text


# 明确不支持流水线并行的架构后缀（编码器/分类器类，这类模型没有跨层切分的中间张量通道）。
# 只列能确证的：引擎侧真正的判据是「模型类是否实现 SupportsPP」，那是运行期按 forward
# 签名反射出来的，源码里没有静态清单，管理端抄不到。所以这里**只做负向排除**——命中才拒，
# 其余（含架构未知）一律放行。误拒一个能跑的部署比漏放一个跑不起来的更糟。
_PP_UNSUPPORTED_ARCH_SUFFIXES: tuple[str, ...] = (
    "ForSequenceClassification",
    "ForTokenClassification",
    "ForMaskedLM",
    "ForImageClassification",
    "ForAudioClassification",
    "ForAudioXVector",
    "ForQuestionAnswering",
    "ForCausalLMWithValueHead",
)

# 非解码器类别：流水线并行按层切分，这些类别在引擎里走的根本不是同一套模型实现。
# 由 ModelType 成员构成（StrEnum 与其字符串值同 hash，故可直接用字符串命中）——类别词汇
# 只此一处，新增/改名不会把本集合留成孤儿。
_PP_UNSUPPORTED_CATEGORIES: frozenset[str] = frozenset(
    {ModelType.EMBEDDING, ModelType.RERANK, ModelType.IMAGE, ModelType.STT, ModelType.TTS}
)


def pp_reject_reason(pp: int, *, architecture: str | None, model_type: str | None) -> str | None:
    """pp>1 与模型不匹配时返回原因，否则 None（架构未知/无法判断 → None）。

    覆盖不了所有情况：真正权威的判据在引擎内部（见文件末尾说明），这里只是把管理端**能确证**
    的那部分提前拦下来，剩下交给引擎裁决。
    """
    if pp <= 1:
        return None
    if model_type and model_type in _PP_UNSUPPORTED_CATEGORIES:
        return f"模型类别「{model_type}」不是解码器，引擎不支持流水线并行（pp={pp}）"
    if architecture and architecture.endswith(_PP_UNSUPPORTED_ARCH_SUFFIXES):
        return f"架构 {architecture} 不支持流水线并行（pp={pp}）；该架构无跨层中间张量通道"
    return None
