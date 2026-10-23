// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/hooks', () => ({
  useDialog: vi.fn(() => ({ close: vi.fn(), confirm: vi.fn(), setProps: vi.fn() })),
}))

import { useDialog } from '@repo/hooks'
import type { Router } from 'vue-router'
import { QUICK_SETUP_PROMPT_KEY, QUICK_SETUP_SKIP_KEY } from '@/api/quick-setup'
import { maybeShowFirstGuide } from '@/components/firstGuide'

const useDialogMock = vi.mocked(useDialog)

/** 取 useDialog 最近一次收到的 options(含 props.onStart/onLater 与 onClose 回调) */
function lastOptions() {
  return useDialogMock.mock.calls.at(-1)?.[0]
}

/** 取 useDialog 最近一次返回的控制器(mock 的 close 供断言) */
function lastHandle() {
  return useDialogMock.mock.results.at(-1)?.value as { close: ReturnType<typeof vi.fn> }
}

const push = vi.fn()
const router = { push } as unknown as Router

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
})

describe('maybeShowFirstGuide(首次部署引导弹窗入口)', () => {
  it('无握手标记(非落地导航/已抑制):不弹窗', () => {
    maybeShowFirstGuide(router)
    expect(useDialogMock).not.toHaveBeenCalled()
  })

  it('有握手标记:消费标记后弹介绍窗,重复调用不重复弹', () => {
    sessionStorage.setItem(QUICK_SETUP_PROMPT_KEY, '1')
    maybeShowFirstGuide(router)
    expect(useDialogMock).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBeNull()
    // 标记一次性:再次调用(如同布局重挂)不再弹
    maybeShowFirstGuide(router)
    expect(useDialogMock).toHaveBeenCalledTimes(1)

    const options = lastOptions()
    // 底部按钮由弹窗内容自绘(描边「稍后再说」+ 实心「开始引导」),不使用 useDialog 默认 footer
    expect(options?.btn).toBe(false)
    expect(options?.dialogProps).toMatchObject({ maskClosable: false })
  })

  it('「开始引导」:关窗并跳转向导页', () => {
    sessionStorage.setItem(QUICK_SETUP_PROMPT_KEY, '1')
    maybeShowFirstGuide(router)
    lastOptions()?.props?.onStart?.()
    expect(lastHandle().close).toHaveBeenCalledTimes(1)
    expect(push).toHaveBeenCalledWith({ name: 'quick-setup' })
  })

  it('「稍后再说」:仅关窗,不跳转', () => {
    sessionStorage.setItem(QUICK_SETUP_PROMPT_KEY, '1')
    maybeShowFirstGuide(router)
    lastOptions()?.props?.onLater?.()
    expect(lastHandle().close).toHaveBeenCalledTimes(1)
    expect(push).not.toHaveBeenCalled()
  })

  it('关闭回调写 skip 会话标记(本会话不再弹,含开始引导路径)', () => {
    sessionStorage.setItem(QUICK_SETUP_PROMPT_KEY, '1')
    maybeShowFirstGuide(router)
    lastOptions()?.onClose?.()
    expect(sessionStorage.getItem(QUICK_SETUP_SKIP_KEY)).toBe('1')
  })
})
