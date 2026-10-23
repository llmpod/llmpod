// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LmLayout from '../../src/components/layout/LmLayout.vue'
import type { LayoutMenuGroup } from '../../src/components/layout/types'

const testMenus: LayoutMenuGroup[] = [
  {
    label: '监控',
    key: 'monitor',
    children: [
      { label: '总览大屏', name: 'page-a' },
      { label: '调用统计', name: 'page-b', disabled: true },
    ],
  },
]

function createTestRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/a', name: 'page-a', component: { template: '<div />' } },
      { path: '/b', name: 'page-b', component: { template: '<div />' } },
      { path: '/config', name: 'config', component: { template: '<div />' } },
    ],
  })
}

function mountLayout(props: Record<string, unknown> = {}, router = createTestRouter()) {
  const wrapper = mount(LmLayout, {
    props: { title: 't', ...props },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

describe('LmLayout 左上角标题溢出兼容', () => {
  it('标题 span 带 title 属性与 truncate 截断类,悬浮显示完整值', () => {
    const { wrapper } = mountLayout({ title: '某超长平台名称有限公司私有化推理集群', menus: [] })
    const span = wrapper.find('span.truncate')
    expect(span.exists()).toBe(true)
    expect(span.attributes('title')).toBe('某超长平台名称有限公司私有化推理集群')
    expect(span.classes()).toContain('min-w-0')
    expect(span.classes()).toContain('whitespace-nowrap')
    wrapper.unmount()
  })

  it('title prop 变化后悬浮 title 响应式跟随', async () => {
    const { wrapper } = mountLayout({ title: '旧名称', menus: [] })
    const span = wrapper.find('span.truncate')
    expect(span.attributes('title')).toBe('旧名称')

    await wrapper.setProps({ title: '新平台名称' })
    expect(span.attributes('title')).toBe('新平台名称')
    expect(span.text()).toBe('新平台名称')

    await nextTick()
    wrapper.unmount()
  })
})

describe('LmLayout 菜单显示设置入口', () => {
  it('未传 menuSettingTo 时不渲染设置按钮', () => {
    const { wrapper } = mountLayout({ menus: testMenus })
    expect(wrapper.find('[aria-label="菜单显示设置"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('传 menuSettingTo 后点击齿轮:跳转到目标路由(设置页菜单子 tab)', async () => {
    const { wrapper, router } = mountLayout({
      menus: testMenus,
      menuSettingTo: { name: 'config', query: { tab: 'menu' } },
    })
    await wrapper.find('[aria-label="菜单显示设置"]').trigger('click')
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('config'))
    expect(router.currentRoute.value.query.tab).toBe('menu')
    wrapper.unmount()
  })
})

describe('LmLayout 菜单置灰(可见性开关下线)', () => {
  it('disabled 菜单项仍渲染但带禁用态样式', () => {
    const { wrapper } = mountLayout({ menus: testMenus })
    expect(wrapper.text()).toContain('总览大屏')
    expect(wrapper.text()).toContain('调用统计')
    const disabledItems = wrapper.findAll('.n-menu-item-content--disabled')
    expect(disabledItems).toHaveLength(1)
    expect(disabledItems[0].text()).toBe('调用统计')
    wrapper.unmount()
  })

  it('点击 disabled 菜单项不跳转,点击正常项可跳转', async () => {
    const { wrapper, router } = mountLayout({ menus: testMenus })
    const items = wrapper.findAll('.n-menu-item-content')
    // 第 1 项 disabled:点击后路由不变化
    await items[1].trigger('click')
    expect(router.currentRoute.value.path).toBe('/')
    // 第 2 项正常:点击后跳转对应路由
    await items[0].trigger('click')
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('page-a'))
    wrapper.unmount()
  })
})

describe('LmLayout 移动端抽屉导航(useBreakpoint 驱动)', () => {
  const originalMatchMedia = window.matchMedia

  function mockMatchMedia(matches: boolean) {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (query: string) => ({
        matches: query === '(max-width: 767px)' && matches,
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => true,
      }),
    })
  }

  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: originalMatchMedia,
    })
  })

  it('窄屏(≤767)渲染汉堡按钮,点击展开抽屉侧栏', async () => {
    mockMatchMedia(true)
    const { wrapper } = mountLayout({ menus: testMenus })
    // 旧实现 onMounted 后需等一帧渲染;useBreakpoint 迁移后首帧即正确,两阶段均覆盖
    await nextTick()
    const burger = wrapper.find('[aria-label="打开菜单"]')
    expect(burger.exists()).toBe(true)
    await burger.trigger('click')
    // 遮罩仅在 isMobile && drawerOpen 时渲染,钉住「点击展开抽屉」路径
    expect(wrapper.find('.lm-layout-mask').exists()).toBe(true)
    wrapper.unmount()
  })

  it('宽屏(>767)不渲染汉堡按钮', () => {
    mockMatchMedia(false)
    const { wrapper } = mountLayout({ menus: testMenus })
    expect(wrapper.find('[aria-label="打开菜单"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('LmLayout 菜单乐观高亮(懒 chunk 下载期即时反馈)', () => {
  /** 局部菜单:两项均可点击(共享 testMenus 的 page-b 带 disabled,不会触发导航) */
  const clickableMenus: LayoutMenuGroup[] = [
    {
      label: '监控',
      key: 'monitor',
      children: [
        { label: '总览大屏', name: 'page-a' },
        { label: '调用统计', name: 'page-b' },
      ],
    },
  ]

  /** gated 懒组件:release 前导航 pending,模拟慢网 chunk 下载;page-a 为已就绪对照路由 */
  function createLazyRouter() {
    let release!: (value: unknown) => void
    const gate = new Promise((resolve) => {
      release = resolve
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/a', name: 'page-a', component: { template: '<div />' } },
        {
          path: '/b',
          name: 'page-b',
          component: () => gate.then(() => ({ template: '<div>lazy-b</div>' })),
        },
      ],
    })
    return { router, releaseLazy: (value?: unknown) => release(value) }
  }

  /** 按文本定位菜单项(不依赖 DOM 顺序)并点击 */
  async function clickMenuItem(wrapper: ReturnType<typeof mountLayout>['wrapper'], text: string) {
    const item = wrapper.findAll('.n-menu-item-content').find(node => node.text() === text)
    expect(item, `菜单项「${text}」应存在`).toBeDefined()
    await item!.trigger('click')
  }

  it('点击菜单瞬间高亮先行(乐观更新),不等懒组件就绪', async () => {
    const { router, releaseLazy } = createLazyRouter()
    const { wrapper } = mountLayout({ menus: clickableMenus }, router)
    // 当前在 /,无高亮;点击调用统计(page-b 懒路由,chunk 未就绪)
    await clickMenuItem(wrapper, '调用统计')
    // push 未 resolve(route.name 未变),但菜单已高亮目标项(naive 内部渲染链跨 tick,waitFor 轮询)
    expect(router.currentRoute.value.name).toBeUndefined()
    await vi.waitFor(() => {
      const selected = wrapper.findAll('.n-menu-item-content--selected')
      expect(selected).toHaveLength(1)
      expect(selected[0].text()).toBe('调用统计')
    })
    // chunk 就绪后导航完成:高亮与真实路由一致
    releaseLazy()
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('page-b'))
    await nextTick()
    await vi.waitFor(() => {
      const selectedAfter = wrapper.findAll('.n-menu-item-content--selected')
      expect(selectedAfter).toHaveLength(1)
      expect(selectedAfter[0].text()).toBe('调用统计')
    })
    wrapper.unmount()
  })

  it('懒组件加载失败(导航 reject):高亮回退到真实路由', async () => {
    let rejectLazy!: (reason: unknown) => void
    const gate = new Promise((_resolve, reject) => {
      rejectLazy = reject
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/b', name: 'page-b', component: () => gate },
      ],
    })
    const { wrapper } = mountLayout({ menus: clickableMenus }, router)
    await clickMenuItem(wrapper, '调用统计')
    // 先确认乐观高亮已生效
    await vi.waitFor(() => {
      expect(wrapper.findAll('.n-menu-item-content--selected')).toHaveLength(1)
    })
    rejectLazy(new Error('chunk load error'))
    await vi.waitFor(() => {
      expect(wrapper.findAll('.n-menu-item-content--selected')).toHaveLength(0)
    })
    wrapper.unmount()
    // push 的 rejection 已被组件内捕获,这里补一次微任务冲洗防 unhandled rejection
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
})

describe('LmLayout 空闲预取路由懒 chunk', () => {
  /** 测试环境无 requestIdleCallback:显式置空钉住 setTimeout 兜底路径;restore 恢复 */
  function stubNoIdleCallback() {
    const original = (window as { requestIdleCallback?: unknown }).requestIdleCallback
    Object.defineProperty(window, 'requestIdleCallback', { configurable: true, value: undefined })
    return () => {
      if (original === undefined) {
        delete (window as { requestIdleCallback?: unknown }).requestIdleCallback
      } else {
        Object.defineProperty(window, 'requestIdleCallback', { configurable: true, value: original })
      }
    }
  }

  it('挂载后空闲触发,懒组件被预加载并写回路由记录(视作已加载)', async () => {
    const restore = stubNoIdleCallback()
    try {
      const loader = vi.fn(() => Promise.resolve({ template: '<div>b</div>' }))
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          { path: '/b', name: 'page-b', component: loader },
        ],
      })
      const { wrapper } = mountLayout({ menus: testMenus }, router)
      // 兜底 setTimeout(500ms) 后触发
      await new Promise((resolve) => setTimeout(resolve, 550))
      expect(loader).toHaveBeenCalledTimes(1)
      // 写回:组件不再是函数,后续导航/进度条判断都视作已加载
      const record = router.resolve({ name: 'page-b' }).matched[0]
      expect(typeof record.components.default).not.toBe('function')
      wrapper.unmount()
    } finally {
      restore()
    }
  })

  it('串行预取:上一 chunk 完成后才调用下一个,慢网不并发抢带宽', async () => {
    const restore = stubNoIdleCallback()
    try {
      const order: string[] = []
      const gated = (tag: string) => {
        let release!: () => void
        const gate = new Promise<void>((resolve) => {
          release = resolve
        })
        const loader = () => {
          order.push(`start:${tag}`)
          return gate.then(() => {
            order.push(`end:${tag}`)
            return { template: `<div>${tag}</div>` }
          })
        }
        return { loader, release }
      }
      const first = gated('first')
      const second = gated('second')
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          { path: '/first', name: 'first', component: first.loader },
          { path: '/second', name: 'second', component: second.loader },
        ],
      })
      const { wrapper } = mountLayout({ menus: testMenus }, router)
      await new Promise((resolve) => setTimeout(resolve, 550))
      // 第一个开始后第二个未开始(串行);释放后第二个才继续
      expect(order).toEqual(['start:first'])
      first.release()
      await new Promise((resolve) => setTimeout(resolve, 20))
      second.release()
      await new Promise((resolve) => setTimeout(resolve, 20))
      expect(order).toEqual(['start:first', 'end:first', 'start:second', 'end:second'])
      wrapper.unmount()
    } finally {
      restore()
    }
  })

  it('预取失败静默忽略,不抛错不重试', async () => {
    const restore = stubNoIdleCallback()
    try {
      const loader = vi.fn(() => Promise.reject(new Error('network offline')))
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          { path: '/b', name: 'page-b', component: loader },
        ],
      })
      const { wrapper } = mountLayout({ menus: testMenus }, router)
      await new Promise((resolve) => setTimeout(resolve, 550))
      expect(loader).toHaveBeenCalledTimes(1)
      // 挂件卸载正常,无 unhandled rejection
      wrapper.unmount()
      await new Promise((resolve) => setTimeout(resolve, 10))
    } finally {
      restore()
    }
  })
})
