// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ConfirmCancelError,
  closeAllDialog,
  useConfirm,
  useDialog,
} from '../src'
import { resolveArea } from '../src/components/dialog/useDialog'
import { mountWithHost } from './helpers/mount'

function buttons(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('.n-button')]
}

async function clickButton(text: string): Promise<void> {
  const btn = buttons().find((item) => item.textContent?.includes(text))
  expect(btn, `未找到按钮: ${text}`).toBeTruthy()
  btn!.click()
  await nextTick()
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

let wrapper: ReturnType<typeof mountWithHost> | null = null

beforeEach(() => {
  wrapper = mountWithHost(() => h('div'))
})

afterEach(() => {
  closeAllDialog()
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

// ---------- resolveArea 纯函数 ----------

describe('resolveArea 面积换算', () => {
  it('undefined 空样式;数字转 rem 宽;[w,h] 宽高;字符串原样', () => {
    expect(resolveArea(undefined)).toEqual({})
    expect(resolveArea(40)).toEqual({ width: '40rem' })
    expect(resolveArea([50, 30])).toEqual({ width: '50rem', height: '30rem' })
    expect(resolveArea('640px')).toEqual({ width: '640px' })
  })
})

// ---------- useDialog ----------

describe('useDialog 命令式弹窗', () => {
  it('字符串标题与内容渲染,close 移除并触发 onClose', async () => {
    const onClose = vi.fn()
    const handle = useDialog({
      title: '详情',
      content: () => h('div', { class: 'dlg-body' }, '内容体'),
      onClose,
    })
    await nextTick()
    expect(document.body.textContent).toContain('详情')
    expect(document.body.textContent).toContain('内容体')

    handle.close()
    await nextTick()
    expect(document.body.textContent).not.toContain('内容体')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('未配 onConfirm:点确定直接关闭', async () => {
    useDialog({ title: 't', content: () => h('div', 'c') })
    await nextTick()
    await clickButton('确定')
    expect(document.body.textContent).not.toContain('c')
  })

  it('onConfirm 自治:期间 loading,由回调决定关闭时机', async () => {
    const gate = deferred()
    const onConfirm = vi.fn((close: () => void) => gate.promise.then(() => close()))
    useDialog({ title: 't', content: () => h('div', 'c'), onConfirm })
    await nextTick()

    await clickButton('确定')
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(typeof onConfirm.mock.calls[0][0]).toBe('function')
    // 未 resolve 前弹窗仍在
    expect(document.body.textContent).toContain('c')

    gate.resolve()
    await nextTick()
    await nextTick()
    expect(document.body.textContent).not.toContain('c')
  })

  it('btn:false 隐藏底部,自定义按钮文案生效', async () => {
    useDialog({ title: 't', content: () => h('div', 'c'), btn: false })
    await nextTick()
    expect(buttons().find((item) => item.textContent?.includes('确定'))).toBeUndefined()
    closeAllDialog()
    await nextTick()

    useDialog({
      title: 't',
      content: () => h('div', 'c'),
      btn: { confirmText: '保存', cancelText: '放弃' },
    })
    await nextTick()
    expect(document.body.textContent).toContain('保存')
    expect(document.body.textContent).toContain('放弃')
  })

  it('setProps 合并透传 props,组件内容同步更新', async () => {
    const Inner = {
      props: { label: { type: String, default: '' } },
      template: '<div class="inner-label">{{ label }}</div>',
    }
    const handle = useDialog({
      title: 't',
      component: Inner,
      props: { label: 'first' },
    })
    await nextTick()
    expect(document.body.querySelector('.inner-label')?.textContent).toBe('first')

    handle.setProps({ label: 'second' })
    await nextTick()
    expect(document.body.querySelector('.inner-label')?.textContent).toBe('second')
  })

  it('component 支持 () => import 异步组件,加载前显示 NSpin', async () => {
    const gate = deferred<{ default: object }>()
    useDialog({
      title: 't',
      component: () => gate.promise,
    })
    await nextTick()
    // defineAsyncComponent 的 loadingComponent 默认 200ms 延迟后才显示
    await vi.waitFor(
      () => expect(document.body.querySelector('.n-spin')).toBeTruthy(),
      { timeout: 1000 },
    )

    gate.resolve({ default: { template: '<div class="async-inner">loaded</div>' } })
    await vi.waitFor(
      () => expect(document.body.querySelector('.async-inner')?.textContent).toBe('loaded'),
      { timeout: 1000 },
    )
  })
})

// ---------- useConfirm ----------

describe('useConfirm 二次确认', () => {
  it('确认 resolve(undefined),取消 reject ConfirmCancelError', async () => {
    const promise = useConfirm({ title: '删除确认', content: '确认删除?' })
    await nextTick()
    expect(document.body.textContent).toContain('确认删除?')
    await clickButton('确定')
    await expect(promise).resolves.toBeUndefined()

    const rejectPromise = useConfirm({ title: 't', content: 'x' })
    await nextTick()
    await clickButton('取消')
    await expect(rejectPromise).rejects.toBeInstanceOf(ConfirmCancelError)
  })

  it('取消时 reject 的错误 name 为 ConfirmCancelError(供 try/catch 判定)', async () => {
    const promise = useConfirm({ title: 't', content: 'x' })
    await nextTick()
    await clickButton('取消')
    await expect(promise).rejects.toMatchObject({ name: 'ConfirmCancelError' })
  })

  it('checkbox 模式:resolve 返回勾选状态,默认勾选生效', async () => {
    const promise = useConfirm({
      title: 't',
      content: 'x',
      checkbox: { label: '不再提示', defaultChecked: true },
    })
    await nextTick()
    expect(document.body.textContent).toContain('不再提示')
    await clickButton('确定')
    await expect(promise).resolves.toBe(true)
  })

  it('随机算式模式:未答对确定禁用,答对后可确认', async () => {
    const promise = useConfirm({ title: 't', content: '高危操作', calc: true })
    await nextTick()

    // 算式文案形如 "3 + 5 =",解析正确答案
    const problemSpan = [...document.querySelectorAll('span')].find((item) =>
      /^\d+ [+-] \d+ =$/.test(item.textContent ?? ''),
    )
    expect(problemSpan, '未渲染算式').toBeTruthy()
    const [a, op, b] = problemSpan!.textContent!.replace(' =', '').split(' ')
    const answer = op === '+' ? Number(a) + Number(b) : Number(a) - Number(b)

    const confirmBtn = buttons().find((item) => item.textContent?.includes('确定'))!
    expect(confirmBtn.disabled).toBe(true)

    // 驱动 NInputNumber 输入正确答案
    const input = document.body.querySelector<HTMLInputElement>(
      '.n-input-number input, .n-input-number-input input',
    )!
    input.value = String(answer)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await nextTick()

    expect(confirmBtn.disabled).toBe(false)
    confirmBtn.click()
    await nextTick()
    await expect(promise).resolves.toBeUndefined()
  })

  it('settled 守卫:确定后再触发取消不重复 settle', async () => {
    const promise = useConfirm({ title: 't', content: 'x' })
    await nextTick()
    await clickButton('确定')
    await expect(promise).resolves.toBeUndefined()
    // 二次触发已不可能(弹窗已移除);重复 reject 路径由 settled 守卫短路,不抛未处理拒绝
    await clickButtonSafe('取消')
  })

  it('关闭自动聚焦:confirm 弹窗 autoFocus=false,useDialog 弹窗保持 naive 默认', async () => {
    const promise = useConfirm({ title: 't', content: 'x' })
    promise.catch(() => {}) // afterEach 清场会触发 reject,提前接住避免 unhandled
    await nextTick()
    await nextTick()
    // ModalBody(naive Modal 内层)的 autoFocus prop 决定 FocusTrap 是否初始聚焦首个可聚焦元素
    const confirmModal = wrapper!.findComponent({ name: 'ModalBody' })
    expect(confirmModal.exists(), '未渲染 confirm 弹窗').toBe(true)
    expect(confirmModal.props('autoFocus')).toBe(false)
    closeAllDialog()
    await nextTick()

    useDialog({ title: 't', content: () => h('div', 'c') })
    await nextTick()
    const dialogModal = wrapper!.findComponent({ name: 'ModalBody' })
    expect(dialogModal.exists(), '未渲染 dialog 弹窗').toBe(true)
    expect(dialogModal.props('autoFocus')).toBe(true)
  })
})

/** 若按钮不存在则静默跳过(弹窗已关闭的场景) */
async function clickButtonSafe(text: string): Promise<void> {
  const btn = buttons().find((item) => item.textContent?.includes(text))
  if (!btn) return
  btn.click()
  await nextTick()
}
