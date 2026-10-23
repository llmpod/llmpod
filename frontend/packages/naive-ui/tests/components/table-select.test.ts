// @vitest-environment happy-dom
import { defineComponent, h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { NDropdown } from 'naive-ui'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LmTableSelect, provideLmCardActionBudget } from '../../src'
import type { LmSelectAction } from '../../src'
/** 全仓库行操作最多的一张表:每行可见 5 项 */
const fiveActions: LmSelectAction[] = [
  { title: '详情' },
  { title: '编辑' },
  { title: '调额' },
  { title: '禁用', danger: true },
  { title: '删除', danger: true },
]

interface MountOptions {
  /** 是否处于卡片内(provide 预算上下文);false = 桌面操作列 */
  inCard?: boolean
  cardWidth?: number
  hasToggle?: boolean
  actions?: LmSelectAction[]
}

function mountSelect(options: MountOptions = {}) {
  const { inCard = true, cardWidth = 364, hasToggle = true, actions = fiveActions } = options
  return mount(
    defineComponent({
      setup() {
        if (inCard) {
          provideLmCardActionBudget({
            cardWidth: ref(cardWidth),
            hasToggle: ref(hasToggle),
            actionGap: 4,
            rowLabel: () => '云启科技',
          })
        }
        return () => h(LmTableSelect, { row: { id: 1, name: '云启科技' }, actions })
      },
    }),
    {
      attachTo: document.body,
      // VTU 默认把 Transition 替换成透传桩,这里要用真组件才能验证进出场结构
      global: { stubs: { transition: false } },
    },
  )
}

/** 面板 teleport 到 body,需从 document 上取 */
const sheet = () => document.querySelector('.lm-action-sheet__panel')

afterEach(() => {
  document.body.style.overflow = ''
})

describe('LmTableSelect 操作项溢出', () => {
  it('卡片内超过预算:只内联前 2 项,其余沉进「更多」', () => {
    const wrapper = mountSelect()
    const labels = wrapper.findAll('.n-button').map((n) => n.text())
    expect(labels[0]).toBe('详情')
    expect(labels[1]).toBe('编辑')
    // 第 3 个按钮是「更多」而不是第三个操作;文案不带数量
    const more = wrapper.find('.lm-table-more')
    expect(more.exists()).toBe(true)
    expect(more.text()).toBe('更多')
    expect(wrapper.findAll('.n-button')).toHaveLength(3)
    wrapper.unmount()
  })

  it('操作数不超过 2 项时全部内联,不渲染「更多」', () => {
    const wrapper = mountSelect({ actions: [{ title: '详情' }, { title: '编辑' }] })
    expect(wrapper.find('.lm-table-more').exists()).toBe(false)
    expect(wrapper.findAll('.n-button')).toHaveLength(2)
    wrapper.unmount()
  })

  it('卡宽 < 240px 时不再内联,只剩「更多」入口', () => {
    const wrapper = mountSelect({ cardWidth: 220 })
    expect(wrapper.find('.lm-table-more').exists()).toBe(true)
    expect(wrapper.findAll('.n-button')).toHaveLength(1)
    wrapper.unmount()
  })

  it('已声明的 more.children 仍然生效,追加在自动溢出项之后', async () => {
    const wrapper = mountSelect({
      cardWidth: 2000,
      actions: [
        { title: '详情' },
        { title: '编辑' },
        { title: '调额' },
        { title: '更多', more: true, children: [{ title: '导出' }] },
      ],
    })
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()
    const items = [...document.querySelectorAll('.lm-action-sheet__item')].map(
      (n) => n.textContent?.trim(),
    )
    expect(items).toEqual(['调额', '导出'])
    wrapper.unmount()
  })

  it('窄屏「更多」打开底部动作面板,危险项置底', async () => {
    const wrapper = mountSelect()
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()

    const panel = sheet()
    expect(panel).toBeTruthy()
    const panelEl = panel as HTMLElement
    // 内联之外的 3 项:调额(安全)+ 禁用/删除(危险)
    const safe = [...panelEl.querySelectorAll('.lm-action-sheet__item:not(.is-danger)')].map(
      (n) => n.textContent?.trim(),
    )
    const danger = [...panelEl.querySelectorAll('.lm-action-sheet__item.is-danger')].map(
      (n) => n.textContent?.trim(),
    )
    expect(safe).toEqual(['调额'])
    expect(danger).toEqual(['禁用', '删除'])
    // 不渲染分组标题
    expect(panelEl.querySelector('.lm-action-sheet__sep')).toBeNull()
    // 危险项排在安全项之后
    const all = [...panelEl.querySelectorAll('.lm-action-sheet__item')].map((n) =>
      n.textContent?.trim(),
    )
    expect(all).toEqual(['调额', '禁用', '删除'])
    // 副标题写明操作对象
    expect(panelEl.querySelector('.lm-action-sheet__hd p')?.textContent?.trim()).toBe('云启科技')
    wrapper.unmount()
  })

  it('面板「取消」与 Esc 都能关闭', async () => {
    const wrapper = mountSelect()
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()
    expect(sheet()).toBeTruthy()

    await (document.querySelector('.lm-action-sheet__cancel') as HTMLElement).click()
    // 退场动画播完才真正移除,不能只等 nextTick
    await vi.waitFor(() => expect(sheet()).toBeFalsy())

    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()
    expect(sheet()).toBeTruthy()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await vi.waitFor(() => expect(sheet()).toBeFalsy())
    wrapper.unmount()
  })

  it('面板内点击操作项会关闭面板并派发 select', async () => {
    const onClick = vi.fn()
    const wrapper = mountSelect({
      actions: [{ title: '详情' }, { title: '编辑' }, { title: '调额', onClick }],
    })
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()
    await (document.querySelector('.lm-action-sheet__item') as HTMLElement).click()
    await vi.waitFor(() => expect(onClick).toHaveBeenCalled())
    await vi.waitFor(() => expect(sheet()).toBeFalsy())
    wrapper.unmount()
  })

  it('进出场动画:遮罩与面板直接挂在 body 下,各自作为 Transition 根节点', async () => {
    // Vue 只在 Transition 的根节点上量过渡时长。若两者共用一个父层包裹,
    // 根节点没有 transition 属性 → 过渡类下一帧就被摘掉,退场动画不会播。
    // 因此这里守住"没有共享包裹层"这个前提(过渡类本身是瞬时的,DOM 模拟器里采样不可靠)。
    const wrapper = mountSelect()
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()

    const scrim = document.querySelector('.lm-action-sheet__scrim')
    const panel = document.querySelector('.lm-action-sheet__panel')
    expect(scrim).toBeTruthy()
    expect(panel).toBeTruthy()
    expect(scrim!.parentElement).toBe(document.body)
    expect(panel!.parentElement).toBe(document.body)

    await (document.querySelector('.lm-action-sheet__cancel') as HTMLElement).click()
    await vi.waitFor(() => expect(sheet()).toBeFalsy())
    expect(document.querySelector('.lm-action-sheet__scrim')).toBeFalsy()
    wrapper.unmount()
  })

  it('不在卡片里(桌面操作列):全部平铺,不自动收纳', () => {
    const wrapper = mountSelect({ inCard: false })
    expect(wrapper.find('.lm-table-more').exists()).toBe(false)
    expect(wrapper.findComponent(NDropdown).exists()).toBe(false)
    // 收纳(行内截断 + 更多)只属于窄屏卡片;正常屏幕 5 项全部内联
    const labels = wrapper.findAll('.n-button').map((n) => n.text())
    expect(labels).toEqual(['详情', '编辑', '调额', '禁用', '删除'])
    wrapper.unmount()
  })

  it('桌面显式声明的 more.children 仍走下拉', () => {
    const wrapper = mountSelect({
      inCard: false,
      actions: [
        { title: '详情' },
        { title: '编辑' },
        { title: '更多', more: true, children: [{ title: '导出' }, { title: '删除', danger: true }] },
      ],
    })
    // 非 more 项全部内联;more 项渲染为下拉入口
    const labels = wrapper.findAll('.n-button').map((n) => n.text())
    expect(labels).toEqual(['详情', '编辑', '更多'])
    const dropdown = wrapper.findComponent(NDropdown)
    expect(dropdown.exists()).toBe(true)
    expect(wrapper.find('.lm-table-more').exists()).toBe(false)
    wrapper.unmount()
  })

  it('面板打开时锁 body 滚动,关闭后还原', async () => {
    const wrapper = mountSelect()
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()
    expect(document.body.style.overflow).toBe('hidden')
    await (document.querySelector('.lm-action-sheet__cancel') as HTMLElement).click()
    await nextTick()
    expect(document.body.style.overflow).toBe('')
    wrapper.unmount()
  })

  it('卸载时清理:面板开着也还原 body 滚动', async () => {
    const wrapper = mountSelect()
    await wrapper.find('.lm-table-more').trigger('click')
    await nextTick()
    expect(document.body.style.overflow).toBe('hidden')
    wrapper.unmount()
    expect(document.body.style.overflow).toBe('')
  })
})

describe('LmTableSelect 异步操作 loading', () => {
  /** 桌面操作列:操作项直接平铺,点击即触发 */
  function mountDesktop(actions: LmSelectAction[]) {
    return mountSelect({ inCard: false, actions })
  }

  /** 取第 index 个内联操作按钮(排除「更多」入口后按声明顺序) */
  function inlineButton(wrapper: ReturnType<typeof mountDesktop>, index: number) {
    return wrapper.findAll('.n-button')[index]
  }

  it('onClick 返回 Promise:按钮转 loading 并禁用,落定后恢复', async () => {
    let resolveClick: () => void = () => {}
    const onClick = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveClick = resolve
        }),
    )
    const wrapper = mountDesktop([{ title: '停止', onClick }])

    const button = inlineButton(wrapper, 0)
    expect(button.classes()).not.toContain('n-button--loading')
    await button.trigger('click')
    await nextTick()

    // Promise 在途:按钮转 loading,连点被拦(naive-ui loading 态拦截 click)
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(button.classes()).toContain('n-button--loading')
    await button.trigger('click')
    await button.trigger('click')
    expect(onClick).toHaveBeenCalledTimes(1)

    resolveClick()
    await vi.waitFor(() => expect(button.classes()).not.toContain('n-button--loading'))
    // 恢复后可再次触发
    await button.trigger('click')
    expect(onClick).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('onClick 返回 rejected Promise:loading 同样恢复,不影响后续点击', async () => {
    let rejectClick: (reason?: unknown) => void = () => {}
    const onClick = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectClick = reject
        }),
    )
    const wrapper = mountDesktop([{ title: '删除', onClick }])

    const button = inlineButton(wrapper, 0)
    await button.trigger('click')
    await nextTick()
    expect(button.classes()).toContain('n-button--loading')

    rejectClick(new Error('boom'))
    await vi.waitFor(() => expect(button.classes()).not.toContain('n-button--loading'))
    // unhandledrejection 由调用方 catch,这里不额外断言
    wrapper.unmount()
  })

  it('onClick 返回非 Promise(同步):按钮不进入 loading', async () => {
    const onClick = vi.fn(() => {})
    const wrapper = mountDesktop([{ title: '详情', onClick }])

    const button = inlineButton(wrapper, 0)
    await button.trigger('click')
    await nextTick()
    expect(onClick).toHaveBeenCalled()
    expect(button.classes()).not.toContain('n-button--loading')
    wrapper.unmount()
  })

  it('一项在 loading 时其余操作不受影响', async () => {
    let resolveClick: () => void = () => {}
    const slowOnClick = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveClick = resolve
        }),
    )
    const fastOnClick = vi.fn()
    const wrapper = mountDesktop([
      { title: '停止', onClick: slowOnClick },
      { title: '详情', onClick: fastOnClick },
    ])

    await inlineButton(wrapper, 0).trigger('click')
    await nextTick()
    // 「停止」在 loading,「详情」按钮仍可正常点击
    expect(inlineButton(wrapper, 1).classes()).not.toContain('n-button--loading')
    await inlineButton(wrapper, 1).trigger('click')
    expect(fastOnClick).toHaveBeenCalledTimes(1)

    resolveClick()
    await vi.waitFor(() =>
      expect(inlineButton(wrapper, 0).classes()).not.toContain('n-button--loading'),
    )
    wrapper.unmount()
  })
})

describe('LmTableSelect 点击后的焦点归属', () => {
  function mountDesktop(actions: LmSelectAction[]) {
    return mountSelect({ inCard: false, actions })
  }

  function inlineButtonEl(wrapper: ReturnType<typeof mountDesktop>, index: number) {
    return wrapper.findAll('.n-button')[index].element as HTMLElement
  }

  it('鼠标点击(detail>0)立即让出焦点:弹窗/抽屉关闭时不再回落到按钮', async () => {
    const onClick = vi.fn()
    const wrapper = mountDesktop([{ title: '详情', onClick }])
    const button = inlineButtonEl(wrapper, 0)
    button.focus()
    expect(document.activeElement).toBe(button)

    // VTU trigger 合成的 click detail=0(等同键盘),鼠标点击需手动派发 detail=1
    button.dispatchEvent(new MouseEvent('click', { detail: 1, bubbles: true }))
    await nextTick()
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(document.activeElement).not.toBe(button)
    wrapper.unmount()
  })

  it('键盘触发(detail=0)不动焦点:保留关闭后回到触发元素的无障碍惯例', async () => {
    const onClick = vi.fn()
    const wrapper = mountDesktop([{ title: '详情', onClick }])
    const button = inlineButtonEl(wrapper, 0)
    button.focus()
    button.dispatchEvent(new MouseEvent('click', { detail: 0, bubbles: true }))
    await nextTick()
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(button)
    wrapper.unmount()
  })
})
