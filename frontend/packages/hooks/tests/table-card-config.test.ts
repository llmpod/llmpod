import { describe, expect, it } from 'vitest'
import { mergeConfig } from '../src/components/table/utils'
import type { TableConfig } from '../src/components/table/table-types'

describe('TableConfig.card 卡片配置', () => {
  it('缺省时 card 默认启用(enabled 未显式为 false)', () => {
    const config = mergeConfig(undefined) as TableConfig
    expect(config.card).toEqual({ enabled: true })
  })

  it('用户配置覆盖默认(浅合并)', () => {
    const config = mergeConfig({ card: { titleKey: 'name', fieldKeys: ['name', 'status'] } }) as TableConfig
    expect(config.card).toEqual({ enabled: true, titleKey: 'name', fieldKeys: ['name', 'status'] })
  })

  it('enabled: false 原样保留(页面显式关闭卡片模式)', () => {
    const config = mergeConfig({ card: { enabled: false } }) as TableConfig
    expect(config.card).toEqual({ enabled: false })
  })
})
