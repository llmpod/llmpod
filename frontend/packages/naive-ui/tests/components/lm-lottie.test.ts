// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LmLottie from '../../src/components/lottie/LmLottie.vue'

/** lottie svg 构建 mock:仅断言传入参数与生命周期调用 */
const { loadAnimation, animMock } = vi.hoisted(() => {
  const animMock = {
    play: vi.fn(),
    pause: vi.fn(),
    stop: vi.fn(),
    destroy: vi.fn(),
    goToAndStop: vi.fn(),
  }
  return { loadAnimation: vi.fn(() => animMock), animMock }
})
vi.mock('lottie-web/build/player/lottie_svg', () => ({
  default: { loadAnimation },
}))

const SAMPLE = { v: '5.7.4', fr: 60, ip: 0, op: 180, w: 200, h: 200, layers: [] }

function stubFetch(ok = true) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok, json: async () => SAMPLE })))
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('LmLottie', () => {
  it('挂载后按需加载:fetch json + loadAnimation(svg 渲染器/loop/autoplay/contain)', async () => {
    stubFetch()
    const wrapper = mount(LmLottie, { props: { src: '/animations/x.json' } })
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>
    expect(fetchMock.mock.calls[0][0]).toBe('/animations/x.json')
    const params = loadAnimation.mock.calls[0][0]
    expect(params.container).toBe(wrapper.find('.lm-lottie__host').element)
    expect(params.renderer).toBe('svg')
    expect(params.loop).toBe(true)
    expect(params.autoplay).toBe(true)
    expect(params.rendererSettings.preserveAspectRatio).toBe('xMidYMid meet')
    expect(params.animationData).toEqual(SAMPLE)
    expect(wrapper.emitted('ready')).toBeTruthy()
  })

  it('fit=slice 透传 preserveAspectRatio', async () => {
    stubFetch()
    mount(LmLottie, { props: { src: '/a.json', fit: 'slice' } })
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    expect(loadAnimation.mock.calls[0][0].rendererSettings.preserveAspectRatio).toBe(
      'xMidYMid slice',
    )
  })

  it('prefers-reduced-motion:不自动播放,跑满一帧后停在首帧(直接停帧会渲染空白)', async () => {
    stubFetch()
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
    mount(LmLottie, { props: { src: '/a.json' } })
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    expect(loadAnimation.mock.calls[0][0].autoplay).toBe(false)
    // 先 play 让渲染器填充形状数据,两帧后再 goToAndStop(0)
    expect(animMock.play).toHaveBeenCalled()
    await vi.waitFor(() => expect(animMock.goToAndStop).toHaveBeenCalledWith(0, true))
  })

  it('加载失败:fallback 插槽保留并发 error,不调用 loadAnimation', async () => {
    stubFetch(false)
    const wrapper = mount(LmLottie, {
      props: { src: '/a.json' },
      slots: { default: '<img data-testid="fallback" src="/x.svg" />' },
    })
    await vi.waitFor(() => expect(wrapper.emitted('error')).toBeTruthy())
    expect(loadAnimation).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="fallback"]').exists()).toBe(true)
  })

  it('卸载:destroy 释放动画实例', async () => {
    stubFetch()
    const wrapper = mount(LmLottie, { props: { src: '/a.json' } })
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    wrapper.unmount()
    expect(animMock.destroy).toHaveBeenCalled()
  })
})

describe('LmLottie trigger=hover', () => {
  /** 精确指针且未开启 reduced-motion(桌面鼠标),单个查询可按需覆盖。
   *  必须打桩 window 上的方法:组件读的是 window.matchMedia,stubGlobal 只改全局引用 */
  function stubMatchMedia(overrides: Record<string, boolean> = {}) {
    const matched: Record<string, boolean> = {
      '(prefers-reduced-motion: reduce)': false,
      ...overrides,
    }
    vi.spyOn(window, 'matchMedia').mockImplementation(
      (query: string) =>
        ({
          matches: matched[query] ?? true,
          media: query,
          onchange: null,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          dispatchEvent: vi.fn(),
        }) as unknown as MediaQueryList,
    )
  }

  it('挂载不动:不 fetch、不 loadAnimation,待首次 mouseenter 才懒加载并接管播放', async () => {
    stubFetch()
    stubMatchMedia()
    const wrapper = mount(LmLottie, {
      props: { src: '/animations/logo.json', trigger: 'hover', loop: false },
      slots: { default: '<svg data-testid="static" />' },
    })
    await Promise.resolve()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    expect(loadAnimation).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="static"]').exists()).toBe(true)

    await wrapper.trigger('mouseenter')
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    // hover 扳机不自动播:实例创建后由 mouseenter 分支接管播放
    expect(loadAnimation.mock.calls[0][0].autoplay).toBe(false)
    expect(loadAnimation.mock.calls[0][0].loop).toBe(false)
    expect(wrapper.find('[data-testid="static"]').exists()).toBe(false)
  })

  it('再次移入从头重播:stop 归零后 play', async () => {
    stubFetch()
    stubMatchMedia()
    const wrapper = mount(LmLottie, { props: { src: '/a.json', trigger: 'hover' } })
    await wrapper.trigger('mouseenter')
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    animMock.stop.mockClear()
    animMock.play.mockClear()
    await wrapper.trigger('mouseenter')
    expect(animMock.stop).toHaveBeenCalledTimes(1)
    expect(animMock.play).toHaveBeenCalledTimes(1)
  })

  it('慢网 hover:加载完成时指针仍在热区内则补播这一次', async () => {
    stubFetch()
    stubMatchMedia()
    const wrapper = mount(LmLottie, { props: { src: '/a.json', trigger: 'hover' } })
    // 加载期间指针未移开(happy-dom 不维护 :hover 状态,此处直接模拟命中)
    vi.spyOn(wrapper.find('.lm-lottie__host').element, 'matches').mockReturnValue(true)
    await wrapper.trigger('mouseenter')
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    // 补齐路径走 replay:stop 归零(首次创建不调 stop)
    await vi.waitFor(() => expect(animMock.stop).toHaveBeenCalledTimes(1))
    expect(animMock.play).toHaveBeenCalledTimes(1)
  })

  it('触屏(无 hover 语义):永不加载,fallback 静帧即终态', async () => {
    stubFetch()
    stubMatchMedia({ '(hover: hover) and (pointer: fine)': false })
    const wrapper = mount(LmLottie, {
      props: { src: '/a.json', trigger: 'hover' },
      slots: { default: '<svg data-testid="static" />' },
    })
    await wrapper.trigger('mouseenter')
    await Promise.resolve()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    expect(loadAnimation).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="static"]').exists()).toBe(true)
  })

  it('prefers-reduced-motion:不加载动画,保持静帧', async () => {
    stubFetch()
    stubMatchMedia({ '(prefers-reduced-motion: reduce)': true })
    mount(LmLottie, { props: { src: '/a.json', trigger: 'hover' } })
    await Promise.resolve()
    expect(loadAnimation).not.toHaveBeenCalled()
  })
})
