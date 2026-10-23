// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, message: { success: vi.fn(), error: vi.fn() }, goToPortalLogin: vi.fn() }
})

vi.mock('@/api/auth', () => ({
  changePassword: vi.fn(),
}))

vi.mock('@/store/auth', () => ({
  useAuthStore: () => authMock,
}))

import { message, goToPortalLogin } from '@repo/hooks'
import { changePassword } from '@/api/auth'
import ChangePasswordDialog from '@/components/ChangePasswordDialog.vue'

const authMock = {
  userInfo: { user_id: 1, username: 'admin' },
  clearToken: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(changePassword).mockResolvedValue({ changed: true })
})

afterEach(() => {
  vi.useRealTimers()
})

function findInput(form: ReturnType<typeof mount>, placeholder: string) {
  return form.findAll('input').find((input) => input.attributes('placeholder')?.includes(placeholder))!
}

type Exposed = { onConfirm: (close: () => void) => Promise<void> }

function mountDialog() {
  const form = mount(ChangePasswordDialog, { attachTo: document.body })
  return { form, exposed: form.vm as unknown as Exposed }
}

describe('ChangePasswordDialog(POST /auth/change-password 对接)', () => {
  it('必填校验:空值提交被拦截,不调接口不关弹窗', async () => {
    const { form, exposed } = mountDialog()
    const close = vi.fn()

    // onConfirm 内部吞掉校验拒绝(弹窗保留),此处断言不关不调
    await exposed.onConfirm(close)
    expect(changePassword).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    form.unmount()
  })

  it('校验通过调改密接口,成功后 toast → 延迟清 token 跳登录', async () => {
    vi.useFakeTimers()
    const { form, exposed } = mountDialog()
    await findInput(form, '当前密码').setValue('old-secret')
    await findInput(form, '新密码').setValue('new-secret-123')

    const close = vi.fn()
    await exposed.onConfirm(close)

    expect(changePassword).toHaveBeenCalledWith({
      old_password: 'old-secret',
      new_password: 'new-secret-123',
    })
    expect(message.success).toHaveBeenCalledWith('密码修改成功,请重新登录')
    expect(close).toHaveBeenCalledTimes(1)
    // token 已失效:延迟 1.2s 后才清 token + 整页跳登录(保证 toast 可见)
    expect(authMock.clearToken).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1200)
    expect(authMock.clearToken).toHaveBeenCalledTimes(1)
    expect(goToPortalLogin).toHaveBeenCalledTimes(1)
    form.unmount()
  })

  it('旧密错误(接口 reject):不提示成功、不关闭弹窗(错误由 HTTP 层提示)', async () => {
    vi.mocked(changePassword).mockRejectedValue(new Error('当前密码错误'))
    const { form, exposed } = mountDialog()
    await findInput(form, '当前密码').setValue('wrong')
    await findInput(form, '新密码').setValue('new-secret')

    const close = vi.fn()
    // onConfirm 内部吞掉接口拒绝(弹窗保留)
    await exposed.onConfirm(close)
    expect(close).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    expect(authMock.clearToken).not.toHaveBeenCalled()
    form.unmount()
  })
})
