// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { afterEach, describe, expect, it } from 'vitest'
import { useLmTableContext } from '@repo/naive-ui'
import { useTable } from '../src/components/table/useTable'
import { buildColumnStorageKey } from '../src/components/table/useTable'

/* eslint-disable @typescript-eslint/no-explicit-any */

interface Row {
  id: number
  name: string
}

const COLUMNS = [
  { title: '名称', key: 'name', hideable: false },
  { title: '状态', key: 'status' },
]

const BASE_OPTIONS = {
  request: async () => ({ data: [] as Row[], total: 0 }),
  columns: COLUMNS,
}

/** 挂载 useTable 并由子组件 inject 捕获 LmTableContext;withRouter=false 模拟无路由环境 */
async function mountTableContext(options: any, withRouter = false, renderColumn = false) {
  let ctx: any
  const App = {
    setup() {
      const api = useTable<Row>(options)
      return () => [
        renderColumn ? h(api.LmColumn) : null,
        h({ setup: () => { ctx = useLmTableContext(); return () => null } }),
      ]
    },
  }
  let wrapper: any
  if (withRouter) {
    const routes: RouteRecordRaw[] = [
      { name: 'user-list', path: '/user', component: { render: () => null } },
    ]
    const router = createRouter({ history: createMemoryHistory(), routes })
    router.push('/user')
    await router.isReady()
    wrapper = mount(App, { global: { plugins: [router] } })
  }
  else {
    wrapper = mount(App)
  }
  await nextTick()
  return { wrapper, ctx }
}

describe('buildColumnStorageKey key 组装规则', () => {
  it('admin/tenant base 生成带平台段的 key', () => {
    expect(buildColumnStorageKey('/admin/', 'user-list')).toBe('llmpod:columns:admin:user-list')
    expect(buildColumnStorageKey('/tenant/', 'orders')).toBe('llmpod:columns:tenant:orders')
  })

  it('根路径 base 省略平台段', () => {
    expect(buildColumnStorageKey('/', 'home')).toBe('llmpod:columns:home')
  })

  it('空/非常规 base 不产生平台段', () => {
    expect(buildColumnStorageKey('', 'home')).toBe('llmpod:columns:home')
    expect(buildColumnStorageKey('/预发布环境/', 'home')).toBe('llmpod:columns:home')
    // 含 . 等非白名单字符的段不作为平台段,避免产生意外 key
    expect(buildColumnStorageKey('/v1.0/', 'home')).toBe('llmpod:columns:home')
  })
})

describe('useTable 列配置持久化 storageKey', () => {
  afterEach(() => localStorage.clear())

  it('无路由且未显式配置时不生成 storageKey(保持不持久化旧行为)', async () => {
    const { wrapper, ctx } = await mountTableContext(BASE_OPTIONS)
    expect(ctx.columnStorageKey).toBeUndefined()
    wrapper.unmount()
  })

  it('有路由时自动生成带路由名的 storageKey(平台段规则见 buildColumnStorageKey 单测)', async () => {
    const { wrapper, ctx } = await mountTableContext(BASE_OPTIONS, true)
    // 测试环境 BASE_URL 为 '/',省略平台段;app 内由 vite base 提供真实平台段
    expect(ctx.columnStorageKey).toMatch(/^llmpod:columns:(.*:)?user-list$/)
    wrapper.unmount()
  })

  it('显式 columnSetting.storageKey 优先于自动生成', async () => {
    const { wrapper, ctx } = await mountTableContext(
      { ...BASE_OPTIONS, config: { columnSetting: { storageKey: 'llmpod:admin:user-list' } } },
      true,
    )
    expect(ctx.columnStorageKey).toBe('llmpod:admin:user-list')
    wrapper.unmount()
  })

  it('刷新恢复:localStorage 记录的隐藏列在 LmColumn 挂载时重新生效', async () => {
    // 直接预写自动生成的 key(BASE_URL='/' 时为 llmpod:columns:user-list)
    localStorage.setItem(
      'llmpod:columns:user-list',
      JSON.stringify({ status: { visible: false } }),
    )
    const { wrapper, ctx } = await mountTableContext(BASE_OPTIONS, true, true)
    expect(ctx.columnStorageKey).toBe('llmpod:columns:user-list')
    expect(ctx.columnVisibleMap.value.status).toBe(false)
    // 锁定列(hideable:false)不受持久化数据影响
    expect(ctx.columnVisibleMap.value.name).toBeUndefined()
    wrapper.unmount()
  })
})
