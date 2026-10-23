// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/hooks', () => ({
  getActivePlatform: vi.fn(),
  goToPlatform: vi.fn(),
}))

import { getActivePlatform, goToPlatform } from '@repo/hooks'
import type { Router } from 'vue-router'
import { setupGuard } from '@/router/guard'

type GuardFn = (to: { name?: string | null }) => unknown

function captureGuard(): GuardFn {
  let guard: GuardFn | null = null
  const router = {
    beforeEach: (fn: GuardFn) => {
      guard = fn
    },
  }
  setupGuard(router as unknown as Router)
  return guard!
}

beforeEach(() => vi.clearAllMocks())

describe('portal router guard', () => {
  it('登录页始终放行(允许已登录用户重新登录覆盖 token)', () => {
    const guard = captureGuard()
    vi.mocked(getActivePlatform).mockReturnValue('admin')
    expect(guard({ name: 'login' })).toBe(true)
    expect(goToPlatform).not.toHaveBeenCalled()
  })

  it('无活跃平台:重定向到登录页', () => {
    const guard = captureGuard()
    vi.mocked(getActivePlatform).mockReturnValue(null)
    expect(guard({ name: 'dispatch' })).toEqual({ name: 'login' })
  })

  it('已登录:整页跳转对应平台且中断 SPA 导航', () => {
    const guard = captureGuard()
    vi.mocked(getActivePlatform).mockReturnValue('tenant')
    expect(guard({ name: 'dispatch' })).toBe(false)
    expect(goToPlatform).toHaveBeenCalledWith('tenant')
  })
})
