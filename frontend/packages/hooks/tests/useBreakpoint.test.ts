// @vitest-environment happy-dom
import { defineComponent, h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { useBreakpoint } from '../src/breakpoint/index'

type MatchListener = (event: MediaQueryListEvent) => void

/**
 * 可控 matchMedia 桩:记录 matches、收集 change 监听,
 * setMatches 模拟断点跨越(翻转 matches 并广播)。
 */
function mockMatchMedia(initialMatches: boolean) {
  const listeners = new Set<MatchListener>()
  const mql = {
    matches: initialMatches,
    media: '(max-width: 767px)',
    onchange: null,
    addEventListener: (_type: string, fn: MatchListener) => listeners.add(fn),
    removeEventListener: (_type: string, fn: MatchListener) => listeners.delete(fn),
    addListener: (fn: MatchListener) => listeners.add(fn),
    removeListener: (fn: MatchListener) => listeners.delete(fn),
    dispatchEvent: () => true,
  }
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => mql,
  })
  return {
    setMatches(matches: boolean) {
      mql.matches = matches
      listeners.forEach((fn) => fn({ matches } as MediaQueryListEvent))
    },
    listenerCount: () => listeners.size,
  }
}

const Host = defineComponent({
  setup() {
    const { isMobile } = useBreakpoint()
    return () => h('div', isMobile.value ? 'mobile' : 'desktop')
  },
})

describe('useBreakpoint', () => {
  it('初始反射断点:matches=true 为移动', () => {
    mockMatchMedia(true)
    const wrapper = mount(Host)
    expect(wrapper.text()).toBe('mobile')
    wrapper.unmount()
  })

  it('初始反射断点:matches=false 为桌面', () => {
    mockMatchMedia(false)
    const wrapper = mount(Host)
    expect(wrapper.text()).toBe('desktop')
    wrapper.unmount()
  })

  it('断点跨越时 isMobile 响应式跟随', async () => {
    const media = mockMatchMedia(false)
    const wrapper = mount(Host)
    expect(wrapper.text()).toBe('desktop')
    media.setMatches(true)
    await nextTick()
    expect(wrapper.text()).toBe('mobile')
    wrapper.unmount()
  })

  it('组件卸载后移除监听:监听数归零,后续广播不影响已卸载实例', async () => {
    const media = mockMatchMedia(false)
    const wrapper = mount(Host)
    expect(media.listenerCount()).toBe(1)
    wrapper.unmount()
    expect(media.listenerCount()).toBe(0)
    expect(() => media.setMatches(true)).not.toThrow()
  })

  it('组件外调用安全:不抛错,isMobile 仍响应断点变化', () => {
    const media = mockMatchMedia(false)
    const { isMobile } = useBreakpoint()
    expect(isMobile.value).toBe(false)
    media.setMatches(true)
    expect(isMobile.value).toBe(true)
  })

  it('无 matchMedia 环境回退桌面(false),不抛错', () => {
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: undefined })
    const wrapper = mount(Host)
    expect(wrapper.text()).toBe('desktop')
    wrapper.unmount()
  })
})
