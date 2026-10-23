// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import LmRouteProgress from '../../src/components/layout/LmRouteProgress.vue'

/** 挂起的懒组件加载器:release 前导航一直 pending,模拟慢网 chunk 下载 */
function gatedLoader() {
  let release!: (value: unknown) => void
  const gate = new Promise((resolve) => {
    release = resolve
  })
  return {
    loader: () => gate.then(() => ({ template: '<div>lazy</div>' })),
    release: (value?: unknown) => release(value),
  }
}

function mountProgress(router: Router) {
  return mount(LmRouteProgress, { global: { plugins: [router] } })
}

function progressBar(wrapper: ReturnType<typeof mountProgress>) {
  return wrapper.find('.lm-route-progress')
}

describe('LmRouteProgress 路由进度条', () => {
  it('目标路由组件已就绪(非懒加载):全程不显示进度条', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/sync', name: 'sync', component: { template: '<div />' } },
      ],
    })
    const wrapper = mountProgress(router)
    await router.push('/sync')
    expect(progressBar(wrapper).classes()).not.toContain('lm-route-progress--visible')
    wrapper.unmount()
  })

  it('懒路由导航超过延迟阈值:进度条出现,导航完成后收尾隐藏', async () => {
    vi.useFakeTimers()
    try {
      const gated = gatedLoader()
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          { path: '/lazy', name: 'lazy', component: gated.loader },
        ],
      })
      const wrapper = mountProgress(router)
      const push = router.push('/lazy')
      // 先冲洗微任务链,让 vue-router guard 链跑到 beforeEach 并注册 150ms 显示定时器
      // (advanceTimersByTimeAsync(0) 冲洗深度不足,手动多轮 Promise.resolve)
      for (let i = 0; i < 10; i++) await Promise.resolve()
      // 阈值(150ms)内:chunk 未就绪但不显示,避免快速导航闪条
      await vi.advanceTimersByTimeAsync(100)
      expect(progressBar(wrapper).classes()).not.toContain('lm-route-progress--visible')
      // 超过阈值:显示
      await vi.advanceTimersByTimeAsync(100)
      await nextTick()
      expect(progressBar(wrapper).classes()).toContain('lm-route-progress--visible')
      // chunk 就绪、导航完成:冲线后隐藏
      gated.release()
      await push
      await vi.advanceTimersByTimeAsync(400)
      await nextTick()
      expect(progressBar(wrapper).classes()).not.toContain('lm-route-progress--visible')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('懒路由在阈值内就绪:进度条全程不出现', async () => {
    vi.useFakeTimers()
    try {
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          { path: '/lazy', name: 'lazy', component: () => Promise.resolve({ template: '<div />' }) },
        ],
      })
      const wrapper = mountProgress(router)
      const push = router.push('/lazy')
      await vi.advanceTimersByTimeAsync(50)
      await push
      await vi.advanceTimersByTimeAsync(500)
      expect(progressBar(wrapper).classes()).not.toContain('lm-route-progress--visible')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('懒组件加载失败(onError):进度条同样收尾,不残留', async () => {
    vi.useFakeTimers()
    try {
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          {
            path: '/fail',
            name: 'fail',
            component: () => Promise.reject(new Error('chunk load error')),
          },
        ],
      })
      const wrapper = mountProgress(router)
      const push = router.push('/fail')
      push.catch(() => undefined)
      await vi.advanceTimersByTimeAsync(400)
      await push.catch(() => undefined)
      await vi.advanceTimersByTimeAsync(400)
      expect(progressBar(wrapper).classes()).not.toContain('lm-route-progress--visible')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('快速连点:前一个懒导航被取代(cancelled)不误杀后一个的进度条', async () => {
    vi.useFakeTimers()
    try {
      const gatedA = gatedLoader()
      const gatedB = gatedLoader()
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { template: '<div />' } },
          { path: '/a', name: 'a', component: gatedA.loader },
          { path: '/b', name: 'b', component: gatedB.loader },
        ],
      })
      const wrapper = mountProgress(router)
      const first = router.push('/a')
      // 冲洗微任务让首个导航的 beforeEach 启动进度条
      for (let i = 0; i < 10; i++) await Promise.resolve()
      // 第二次导航取代第一次:旧导航的 afterEach(cancelled) 不应收尾新进度条
      const second = router.push('/b')
      await vi.advanceTimersByTimeAsync(200)
      await nextTick()
      expect(progressBar(wrapper).classes()).toContain('lm-route-progress--visible')
      gatedB.release()
      await second
      await vi.advanceTimersByTimeAsync(400)
      await nextTick()
      expect(progressBar(wrapper).classes()).not.toContain('lm-route-progress--visible')
      gatedA.release()
      first.catch(() => undefined)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
