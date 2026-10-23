// @vitest-environment happy-dom
import { h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import LmPlayground from '../../src/components/playground/LmPlayground.vue'
import { mountWithProvider } from '../helpers/mount'

/** 挂载完整 LmPlayground,返回 wrapper 与 sender spy。 */
function mountPlayground(props: Record<string, unknown> = {}) {
  const sender = vi.fn(async () => {})
  const wrapper = mountWithProvider(() =>
    h(LmPlayground, {
      models: [{ label: 'm1', value: 'm1' }],
      sender,
      ...props,
    }),
  )
  return { wrapper, sender }
}

/** 原因 tooltip 是否可见(内容传送 body;隐藏后节点残留 display:none,不能只查 textContent)。 */
function tipVisible(text: string) {
  return Array.from(document.querySelectorAll('.n-popover')).some(
    (el) => (el as HTMLElement).style.display !== 'none' && (el.textContent ?? '').includes(text),
  )
}

/** 等 naive popover 受控 show 渲染到 body。 */
async function flushTicks() {
  await nextTick()
  await nextTick()
  await nextTick()
}

/** 底部输入框 textarea(排除左侧系统提示词的那一个)。 */
function composerTextarea(wrapper: ReturnType<typeof mount>) {
  return wrapper.find('.lm-playground__input textarea')
}

describe('LmPlayground 发送禁用提示', () => {
  it('未选 API Key:hover 发送按钮出现原因提示,移开消失', async () => {
    const { wrapper } = mountPlayground({
      apiKeys: [{ label: 'k1', value: '1' }],
      apiKey: null,
    })

    const btn = wrapper.find('.lm-playground__send-btn')
    expect(btn.classes()).toContain('lm-playground__send-btn--disabled')

    await wrapper.find('.lm-playground__send-wrap').trigger('mouseenter')
    await flushTicks()
    expect(tipVisible('请先选择 API Key')).toBe(true)

    await wrapper.find('.lm-playground__send-wrap').trigger('mouseleave')
    await flushTicks()
    expect(tipVisible('请先选择 API Key')).toBe(false)

    wrapper.unmount()
  })

  it('输入为空:Enter 触发原因提示,不调用 sender', async () => {
    const { wrapper, sender } = mountPlayground()

    await composerTextarea(wrapper).trigger('keydown', { key: 'Enter' })
    await flushTicks()
    expect(tipVisible('请输入消息内容')).toBe(true)
    expect(sender).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('可发送:hover 不出现原因提示,Enter 正常发送', async () => {
    const { wrapper, sender } = mountPlayground()

    await composerTextarea(wrapper).setValue('你好')
    expect(wrapper.find('.lm-playground__send-btn').classes()).not.toContain(
      'lm-playground__send-btn--disabled',
    )

    await wrapper.find('.lm-playground__send-wrap').trigger('mouseenter')
    await flushTicks()
    expect(tipVisible('请输入消息内容')).toBe(false)

    await composerTextarea(wrapper).trigger('keydown', { key: 'Enter' })
    expect(sender).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })

  it('requireApiKey:Key 列表为空也禁用发送,Enter 提示创建;未开启时空列表可发送', async () => {
    // 开启(租户端):空列表 + 已有输入,按钮仍禁用,Enter 提示创建 Key 且不调用 sender
    const required = mountPlayground({ requireApiKey: true })
    await composerTextarea(required.wrapper).setValue('你好')
    expect(required.wrapper.find('.lm-playground__send-btn').classes()).toContain(
      'lm-playground__send-btn--disabled',
    )

    await composerTextarea(required.wrapper).trigger('keydown', { key: 'Enter' })
    await flushTicks()
    expect(tipVisible('请先创建 API Key')).toBe(true)
    expect(required.sender).not.toHaveBeenCalled()
    required.wrapper.unmount()

    // 未开启(默认,admin token 模式):空列表 + 有输入即可正常发送
    const plain = mountPlayground()
    await composerTextarea(plain.wrapper).setValue('你好')
    expect(plain.wrapper.find('.lm-playground__send-btn').classes()).not.toContain(
      'lm-playground__send-btn--disabled',
    )

    await composerTextarea(plain.wrapper).trigger('keydown', { key: 'Enter' })
    expect(plain.sender).toHaveBeenCalledTimes(1)
    plain.wrapper.unmount()
  })
})

describe('LmPlayground 初始模型选中', () => {
  /**
   * 当前选中模型值。apiKeys 为空时第一个 Select 即「模型」(其后是思考强度);
   * 直接读组件 value prop,绕开 filterable select 的 input 展示差异。
   * 未选中时 LmSelect 会把空值归一化为 null(见 form-fields.test.ts)。
   */
  function selectedModel(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAllComponents({ name: 'Select' })[0].props('value') as string | null
  }

  it('未传 initialModel:默认选中第一个模型', () => {
    const { wrapper } = mountPlayground({
      models: [
        { label: 'm1', value: 'm1' },
        { label: 'm2', value: 'm2' },
      ],
    })
    expect(selectedModel(wrapper)).toBe('m1')
    wrapper.unmount()
  })

  it('initialModel 匹配选项:优先选中该模型(而非第一个)', () => {
    const { wrapper } = mountPlayground({
      models: [
        { label: 'm1', value: 'm1' },
        { label: 'm2', value: 'm2' },
      ],
      initialModel: 'm2',
    })
    expect(selectedModel(wrapper)).toBe('m2')
    wrapper.unmount()
  })

  it('模型列表异步加载完成后:选中 initialModel 对应模型', async () => {
    const models = ref<{ label: string; value: string }[]>([])
    const wrapper = mountWithProvider(() =>
      h(LmPlayground, {
        models: models.value,
        sender: vi.fn(async () => {}),
        initialModel: 'm2',
      }),
    )
    await nextTick()
    // 模型列表为空:params.model 为空值,经 LmSelect 归一化为 null 传给内部 select
    expect(selectedModel(wrapper)).toBeNull()

    models.value = [
      { label: 'm1', value: 'm1' },
      { label: 'm2', value: 'm2' },
    ]
    await nextTick()
    expect(selectedModel(wrapper)).toBe('m2')
    wrapper.unmount()
  })

  it('initialModel 不在选项中:回退选第一个', () => {
    const { wrapper } = mountPlayground({
      models: [
        { label: 'm1', value: 'm1' },
        { label: 'm2', value: 'm2' },
      ],
      initialModel: 'not-exist',
    })
    expect(selectedModel(wrapper)).toBe('m1')
    wrapper.unmount()
  })
})

describe('LmPlayground 必填星号与错误态', () => {
  /** 左侧面板全部 n-select 根节点(顺序:API Key → 模型 → 思考强度)。 */
  function selectNodes(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAll('.lm-playground .n-base-selection')
  }

  it('星号:模型恒必填;API Key 字段展示时(列表非空)同样必填', () => {
    // 无 API Key 字段:第一个 label 是模型,恒带星号
    const plain = mountPlayground()
    const plainLabels = plain.wrapper.findAll('.lm-playground__label')
    expect(plainLabels[0].classes()).toContain('lm-playground__label--required')
    plain.wrapper.unmount()

    // 有 API Key 字段:两个字段都带星号
    const withKeys = mountPlayground({ apiKeys: [{ label: 'k1', value: '1' }], apiKey: null })
    const keyLabels = withKeys.wrapper.findAll('.lm-playground__label')
    expect(keyLabels[0].classes()).toContain('lm-playground__label--required')
    expect(keyLabels[1].classes()).toContain('lm-playground__label--required')
    withKeys.wrapper.unmount()
  })

  it('Enter 被拦:未选 API Key 的 select 变红,已选模型的 select 不红', async () => {
    const { wrapper } = mountPlayground({ apiKeys: [{ label: 'k1', value: '1' }], apiKey: null })
    await composerTextarea(wrapper).setValue('你好')

    await composerTextarea(wrapper).trigger('keydown', { key: 'Enter' })
    await flushTicks()
    const selects = selectNodes(wrapper)
    expect(selects[0].classes()).toContain('n-base-selection--error-status')
    expect(selects[1].classes()).not.toContain('n-base-selection--error-status')
    expect(tipVisible('请先选择 API Key')).toBe(true)

    wrapper.unmount()
  })

  it('点击禁用发送按钮:同样触发红框与原因提示,不调用 sender', async () => {
    const { wrapper, sender } = mountPlayground({ apiKeys: [{ label: 'k1', value: '1' }], apiKey: null })
    await composerTextarea(wrapper).setValue('你好')

    await wrapper.find('.lm-playground__send-btn').trigger('click')
    await flushTicks()
    expect(selectNodes(wrapper)[0].classes()).toContain('n-base-selection--error-status')
    expect(tipVisible('请先选择 API Key')).toBe(true)
    expect(sender).not.toHaveBeenCalled()

    wrapper.unmount()
  })
})

describe('LmPlayground 窄屏(≤767px)参数面板抽屉', () => {
  /** 可控 matchMedia 桩(精确匹配 useBreakpoint 的 MOBILE_QUERY) */
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

  it('窄屏渲染「参数」开关,点击展开面板(带 --open 类)与遮罩;点遮罩收起', async () => {
    mockMatchMedia(true)
    const { wrapper } = mountPlayground()

    const toggle = wrapper.find('.lm-playground__panel-toggle')
    expect(toggle.exists()).toBe(true)
    expect(wrapper.find('.lm-playground__panel--open').exists()).toBe(false)

    await toggle.trigger('click')
    expect(wrapper.find('.lm-playground__panel--open').exists()).toBe(true)
    const mask = wrapper.find('.lm-playground__mask')
    expect(mask.exists()).toBe(true)

    await mask.trigger('click')
    expect(wrapper.find('.lm-playground__panel--open').exists()).toBe(false)
    expect(wrapper.find('.lm-playground__mask').exists()).toBe(false)

    wrapper.unmount()
  })

  it('宽屏不渲染「参数」开关,面板始终常驻(无 --open 类)', () => {
    mockMatchMedia(false)
    const { wrapper } = mountPlayground()
    expect(wrapper.find('.lm-playground__panel-toggle').exists()).toBe(false)
    expect(wrapper.find('.lm-playground__panel').exists()).toBe(true)
    expect(wrapper.find('.lm-playground__panel--open').exists()).toBe(false)
    wrapper.unmount()
  })
})
