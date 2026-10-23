import { describe, expect, it } from 'vitest'
import {
  ACTION_BUDGET,
  CARD_ACTION_METRIC,
  CELL_ACTION_METRIC,
  estimateActionWidth,
  estimateOperateWidth,
  fallbackInlineCount,
  resolveCardInlineCount,
  resolveOperateWidth,
} from '../../src'

describe('行操作内联预算', () => {
  /** 全仓库行操作最多的一张表:详情/编辑/调额/禁用/启用/删除(禁用↔启用互斥,每行可见 5 项) */
  const fiveLabels = ['详情', '编辑', '调额', '禁用', '删除']

  it('按钮估宽:汉字与拉丁分别累加,再补左右内边距', () => {
    expect(estimateActionWidth('详情')).toBe(46) // 26 + 20
    expect(estimateActionWidth('ok')).toBe(35) // 15 + 20
    // 操作列的 text 按钮无内边距、字号 12px
    expect(estimateActionWidth('详情', CELL_ACTION_METRIC)).toBe(24)
    expect(estimateActionWidth('详情', CARD_ACTION_METRIC)).toBe(46)
  })

  it('390px 真机的 364px 卡宽:内联 2 项,其余下沉', () => {
    expect(resolveCardInlineCount(364, fiveLabels, true)).toBe(2)
  })

  it('卡宽 < 240px:退化为「展开 + 更多」两个入口,永不顶出卡片', () => {
    expect(resolveCardInlineCount(239, fiveLabels, true)).toBe(0)
    // 240px 刚好放下第一项
    expect(resolveCardInlineCount(240, fiveLabels, true)).toBe(1)
  })

  it('卡宽继续收窄:内联项数单调不增', () => {
    const widths = [500, 364, 300, 289, 280, 240, 200]
    const counts = widths.map((w) => resolveCardInlineCount(w, fiveLabels, true))
    for (let i = 1; i < counts.length; i++) {
      expect(counts[i]).toBeLessThanOrEqual(counts[i - 1])
    }
  })

  it('内联上限固定 2 项 —— 再宽也不内联第三项(与桌面端列宽同一口径)', () => {
    expect(resolveCardInlineCount(2000, fiveLabels, true)).toBe(ACTION_BUDGET.inlineCap)
  })

  it('操作列估宽:按全量可见操作平铺累加(桌面不收纳),与声明列宽取 max 兜底', () => {
    // 5 项双字操作 = 5×24 + 4×gap12 = 168,再补 td 左右 padding 24 → 192
    expect(estimateOperateWidth(fiveLabels)).toBe(168)
    expect(resolveOperateWidth(120, fiveLabels)).toBe(192)
    // 声明 200(刻意留宽)不被估宽收窄
    expect(resolveOperateWidth(200, fiveLabels)).toBe(200)
    // 单操作:24 + 24 = 48 < 90,纯声明生效
    expect(resolveOperateWidth(90, ['详情'])).toBe(90)
    expect(resolveOperateWidth(90, [])).toBe(90)
  })

  it('操作数不超过上限时不预留「更多」入口,全部内联', () => {
    expect(resolveCardInlineCount(364, ['详情', '编辑'], true)).toBe(2)
    expect(resolveCardInlineCount(364, ['详情'], true)).toBe(1)
  })

  it('「展开」入口占位会挤压操作区', () => {
    expect(resolveCardInlineCount(250, fiveLabels, true)).toBeLessThanOrEqual(
      resolveCardInlineCount(250, fiveLabels, false),
    )
  })

  it('间距参与预算:间距越大,能内联的项数不会更多', () => {
    const tight = resolveCardInlineCount(300, fiveLabels, true, 4)
    const loose = resolveCardInlineCount(300, fiveLabels, true, 12)
    expect(loose).toBeLessThanOrEqual(tight)
  })

  it('桌面操作列不再按单元格宽切片 —— 收纳只属于窄屏卡片(见 table-select.test.ts)', () => {
    // resolveCellInlineCount 已随桌面自动收纳一并移除,列宽由 resolveOperateWidth 兜底
    expect(resolveOperateWidth(120, fiveLabels)).toBeGreaterThanOrEqual(5 * 24)
  })

  it('未测量宽度时回退到内联上限', () => {
    expect(fallbackInlineCount(5)).toBe(2)
    expect(fallbackInlineCount(1)).toBe(1)
  })
})
