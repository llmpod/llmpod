import { describe, expect, it, vi } from 'vitest'
import { collectSearchActions, collectSubTabs } from '../../src/components/global-search/collect-actions'

describe('collectSearchActions 操作聚合', () => {
  const modules = {
    '../views/team/search.ts': {
      searchActions: [
        { action: 'create', label: '新建成员', keywords: ['成员', '邀请'] },
      ],
    },
    '../views/secret-page/search.ts': {
      searchActions: [{ action: 'danger', label: '高危操作' }],
    },
  }

  it('目录名解析路由 name,按 hasMenu 过滤,onSelect 带 ?action 跳转', () => {
    const navigate = vi.fn()
    const items = collectSearchActions(modules, {
      hasMenu: (name) => name === 'team',
      navigate,
      pageLabel: (name) => (name === 'team' ? '团队' : undefined),
    })

    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      key: 'action:team:create',
      label: '新建成员',
      group: '操作',
      meta: '团队',
      keywords: ['成员', '邀请'],
    })

    items[0].onSelect()
    expect(navigate).toHaveBeenCalledWith({ name: 'team', query: { action: 'create' } })
  })

  it('无权限页面被过滤;路径不匹配命名约定时忽略', () => {
    const navigate = vi.fn()
    const items = collectSearchActions(
      {
        ...modules,
        '../other/team/search.ts': { searchActions: [{ action: 'x', label: 'x' }] },
      },
      { hasMenu: () => true, navigate },
    )
    // ../other 不匹配 /views/<name>/search.ts 约定,被忽略
    expect(items.map((item) => item.key)).toEqual(['action:team:create', 'action:secret-page:danger'])
  })

  it('searchActions 未声明时返回空', () => {
    const items = collectSearchActions(
      { '../views/empty/search.ts': {} as never },
      { hasMenu: () => true, navigate: vi.fn() },
    )
    expect(items).toEqual([])
  })
})

describe('collectSubTabs 子 tab 聚合', () => {
  const modules = {
    '../views/cost/tabs.ts': {
      searchTabs: [
        { tab: 'invoice', label: '发票管理', keywords: ['发票'] },
        { tab: 'bill', label: '账单' },
      ],
    },
  }

  it('目录名解析父页面,pageGroup 提供分组/图标,onSelect 带 ?tab 跳转', () => {
    const navigate = vi.fn()
    const icon = { render: () => null }
    const items = collectSubTabs(modules, {
      hasMenu: () => true,
      navigate,
      pageGroup: (name) => (name === 'cost' ? { label: '费用', icon } : undefined),
      pageLabel: (name) => (name === 'cost' ? '费用中心' : undefined),
    })

    expect(items).toHaveLength(2)
    expect(items[0]).toMatchObject({
      key: 'tab:cost:invoice',
      label: '发票管理',
      group: '费用',
      meta: '费用中心',
      keywords: ['发票'],
    })
    expect(items[0].icon).toBe(icon)

    items[1].onSelect()
    expect(navigate).toHaveBeenCalledWith({ name: 'cost', query: { tab: 'bill' } })
  })

  it('无 pageGroup 回退默认分组名「页面」且无图标;无权限父页面被过滤', () => {
    const items = collectSubTabs(modules, {
      hasMenu: () => true,
      navigate: vi.fn(),
    })
    expect(items[0].group).toBe('页面')
    expect(items[0].icon).toBeUndefined()
    expect(items[0].meta).toBeUndefined()

    const none = collectSubTabs(modules, {
      hasMenu: () => false,
      navigate: vi.fn(),
    })
    expect(none).toEqual([])
  })
})
