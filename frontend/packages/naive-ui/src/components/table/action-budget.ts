import type { InjectionKey, Ref } from 'vue'
import { inject, provide } from 'vue'

/**
 * 行操作内联预算 —— 操作项放不下时沉进「更多」,而不是把文字挤小或顶出容器。
 *
 * 常量与算式取自移动端原型 R6 / M1:预算是按**容器实测宽度**算出来的,不是拍出来的。
 *
 * 现状对照:5 项全内联 = 页脚内边距 20 + 「展开」88 + 间距 4 + 5×42 + 4×12 = 370px,
 * 已超过 390px 真机的卡宽 364px;360px 机型只剩 334px,溢出 36px。
 *
 * 收纳只属于窄屏卡片;桌面操作列全量平铺,列宽由 resolveOperateWidth 兜底。
 */
export const ACTION_BUDGET = {
  /** 卡片页脚左右内边距(1.2rem + .8rem) */
  footPad: 20,
  /** 「展开 N 项」入口估宽 */
  toggleW: 88,
  /** 「更多」入口估宽(含箭头)。按原型 R6 口径保留 74:当前入口文案不带计数(实测约 64),
   * 略偏保守 —— 只会少内联一项,不会溢出 */
  moreW: 74,
  /** 相邻按钮间距 */
  gap: 4,
  /** 内联操作上限(仅窄屏卡片收纳生效;桌面全量平铺不受限) */
  inlineCap: 2,
} as const

/** 桌面操作列相邻按钮间距(px):与 LmTableSelect 的 gap 默认值同源,估宽与渲染必须一致 */
export const CELL_ACTION_GAP = 12

/** 桌面操作列 td 左右内边距合计(px):naive 默认 medium 12px×2,估宽兜底需补足 */
const CELL_TD_PADDING = 24

/** 按钮估宽口径:字号与左右内边距,必须与真实渲染一致,否则估算与实际不符 */
export interface ActionMetric {
  /** 单个汉字宽度(px) */
  cjk: number
  /** 单个拉丁字符宽度(px) */
  latin: number
  /** 左右内边距合计(px) */
  padding: number
}

/** 卡片页脚按钮:13px 字 + 约 20px 左右内边距(原型 R6 口径,略保守) */
export const CARD_ACTION_METRIC: ActionMetric = { cjk: 13, latin: 7.5, padding: 20 }

/** 操作列按钮:12px(text-base)的 n-button text,无左右内边距 */
export const CELL_ACTION_METRIC: ActionMetric = { cjk: 12, latin: 7, padding: 0 }

/** 按钮估宽:逐字累加再补内边距 */
export function estimateActionWidth(
  label: string,
  metric: ActionMetric = CARD_ACTION_METRIC,
): number {
  let width = 0
  for (const char of label) width += /[\u4e00-\u9fa5]/.test(char) ? metric.cjk : metric.latin
  return Math.ceil(width) + metric.padding
}

/** 从左往右装操作,返回能内联的项数(上限 inlineCap);第 1 项都放不下时返回 0 */
function packInline(room: number, labels: string[], gap: number, metric: ActionMetric): number {
  if (room <= 0) return 0
  let used = 0
  let count = 0
  for (const label of labels) {
    if (count >= ACTION_BUDGET.inlineCap) break
    const next = used + estimateActionWidth(label, metric) + gap
    if (next > room) break
    used = next
    count += 1
  }
  return count
}

/**
 * 卡片页脚:按实测卡宽求内联项数。
 * 先扣掉不可压缩的开销(页脚内边距、「展开」入口),溢出时再给「更多」入口留位。
 *
 * @param gap 相邻操作按钮间距 —— 必须与渲染用的间距一致,否则估算与实际不符
 */
export function resolveCardInlineCount(
  cardWidth: number,
  labels: string[],
  hasToggle: boolean,
  gap: number = ACTION_BUDGET.gap,
): number {
  const { footPad, toggleW, moreW, inlineCap } = ACTION_BUDGET
  const room
    = cardWidth
      - footPad
      - (hasToggle ? toggleW + gap : 0)
      - (labels.length > inlineCap ? moreW + gap : 0)
  return packInline(room, labels, gap, CARD_ACTION_METRIC)
}

/** 桌面操作列不再按单元格宽收纳 —— 收纳(行内截断 + 「更多」)只属于窄屏卡片。
 *  全量可见操作平铺的估宽,供 useOperate 与声明列宽取 max 兜底:
 *  桌面全平铺后,按「2 项 + 更多」定的旧列宽装不下,业务侧不必逐页改。 */
export function estimateOperateWidth(labels: string[], gap: number = CELL_ACTION_GAP): number {
  if (!labels.length) return 0
  return (
    labels.reduce((sum, label) => sum + estimateActionWidth(label, CELL_ACTION_METRIC), 0)
    + (labels.length - 1) * gap
  )
}

/** 桌面操作列宽:声明值与平铺估宽取 max,再补足 td 左右内边距 */
export function resolveOperateWidth(
  declared: number,
  labels: string[],
  gap: number = CELL_ACTION_GAP,
): number {
  return Math.max(declared, estimateOperateWidth(labels, gap) + CELL_TD_PADDING)
}

/** 未测量时的回退:按内联上限取,避免首帧把所有操作都挤进「更多」 */
export function fallbackInlineCount(total: number): number {
  return Math.min(total, ACTION_BUDGET.inlineCap)
}

/** 卡片向行操作注入的预算上下文(LmTableCard provide → LmTableSelect inject) */
export interface LmCardActionBudget {
  /** 卡片实测宽度(px);0 = 尚未测量,消费方回退到内联上限 */
  cardWidth: Ref<number>
  /** 卡片是否渲染了「展开 N 项」入口(该入口占位会挤压操作区) */
  hasToggle: Ref<boolean>
  /** 卡片内相邻操作按钮间距(px):预算与渲染必须取同一个值 */
  actionGap?: number
  /** 行标识纯文本(动作面板副标题用,写明"这是对哪一行");取不到时返回空串 */
  rowLabel?: (row: unknown) => string
}

export const LM_CARD_ACTION_BUDGET_KEY: InjectionKey<LmCardActionBudget> = Symbol(
  'lm-card-action-budget',
)

/** inject 辅助:无卡片祖先时返回 undefined(此时按桌面操作列处理) */
export function useLmCardActionBudget(): LmCardActionBudget | undefined {
  return inject(LM_CARD_ACTION_BUDGET_KEY, undefined)
}

/** provide 辅助 */
export function provideLmCardActionBudget(budget: LmCardActionBudget): void {
  provide(LM_CARD_ACTION_BUDGET_KEY, budget)
}
