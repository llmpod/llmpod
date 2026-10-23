// @vitest-environment happy-dom
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    message: { success: vi.fn(), error: vi.fn() },
    goToPortalLogin: vi.fn(),
    useConfirm: vi.fn(),
  }
})

vi.mock('@/store/auth', () => ({
  useAuthStore: () => authMock,
}))

vi.mock('@/api/team', () => ({
  getTenantUser: vi.fn(),
  updateTenantUser: vi.fn(),
}))

vi.mock('@/api/auth', () => ({
  changePassword: vi.fn(),
}))

import { message, goToPortalLogin, useConfirm } from '@repo/hooks'
import { getTenantUser, updateTenantUser, type TenantUserData } from '@/api/team'
import { changePassword } from '@/api/auth'
import { useController } from '@/views/config/useController'
import { searchTabs } from '@/views/config/tabs'

const authMock = {
  userInfo: { user_id: 1, username: 'tester' } as { user_id: number; username: string } | null,
  clearToken: vi.fn(),
}

function makeMember(overrides: Partial<TenantUserData> = {}): TenantUserData {
  return {
    id: 1,
    tenant_id: 1,
    username: 'tester',
    email: 'tester@x.com',
    phone: '13800000000',
    status: 'active',
    last_login_at: null,
    created_at: '',
    updated_at: '',
    role_ids: [],
    tenant_balance: '0',
    tenant_frozen_balance: '0',
    tenant_available: '0',
    ...overrides,
  } as TenantUserData
}

beforeEach(() => {
  vi.clearAllMocks()
  authMock.userInfo = { user_id: 1, username: 'tester' }
  vi.mocked(getTenantUser).mockResolvedValue(makeMember())
  vi.mocked(updateTenantUser).mockResolvedValue(makeMember())
  vi.mocked(changePassword).mockResolvedValue({ changed: true })
  // 修改密码需 2 次确认:默认都放行
  vi.mocked(useConfirm).mockResolvedValue(undefined)
})

function mountController() {
  let controller!: ReturnType<typeof useController>
  const wrapper = mount({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  return { controller, wrapper }
}

/** 填好旧密/新密并点「修改密码」 */
async function fillPasswordAndSubmit(form: ReturnType<typeof mount>) {
  await form.findAll('input').find((input) => input.attributes('placeholder')?.includes('当前密码'))!
    .setValue('old-secret')
  await form.findAll('input').find((input) => input.attributes('placeholder')?.includes('新密码'))!
    .setValue('new-secret-123')
  const submitBtn = form.findAll('button').find((btn) => btn.text().includes('修改密码'))!
  await submitBtn.trigger('click')
}

describe('config useController 个人信息表单', () => {
  it('邮箱非法:提交被校验拦截,显示反馈且不提示成功', async () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.ProfileForm, { attachTo: document.body })

    const emailInput = form.findAll('input').find((input) =>
      input.attributes('placeholder')?.includes('邮箱'),
    )!
    // 等挂载回填完成再输入,避免异步回填覆盖测试值
    await vi.waitFor(() => {
      expect((emailInput.element as HTMLInputElement).value).toBe('tester@x.com')
    })
    // 先填非法值再点击:提交期间 disableOnSubmitting 会级联禁用字段,禁用态忽略输入事件
    await emailInput.setValue('not-an-email')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存修改'))!
    await saveBtn.trigger('click')

    await vi.waitFor(() => {
      expect(form.findAll('.n-form-item-feedback').map((f) => f.text())).toContain('请输入正确的邮箱格式')
    })
    expect(message.success).not.toHaveBeenCalled()
    expect(updateTenantUser).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('邮箱合法:调 updateTenantUser 保存资料并提示成功', async () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.ProfileForm, { attachTo: document.body })

    const emailInput = form.findAll('input').find((input) =>
      input.attributes('placeholder')?.includes('邮箱'),
    )!
    // 等挂载回填完成再输入,避免异步回填覆盖测试值
    await vi.waitFor(() => {
      expect((emailInput.element as HTMLInputElement).value).toBe('tester@x.com')
    })
    await emailInput.setValue('valid@x.com')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存修改'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('个人信息已保存'))
    // phone 为挂载回填的成员资料值
    expect(updateTenantUser).toHaveBeenCalledWith(1, { email: 'valid@x.com', phone: '13800000000' })
    wrapper.unmount()
  })

  it('挂载后经 getTenantUser 回填 username/email/phone', async () => {
    vi.mocked(getTenantUser).mockResolvedValue(
      makeMember({ username: 'someone', email: 'a@b.c', phone: '13900000000' }),
    )
    const { controller, wrapper } = mountController()
    const form = mount(controller.ProfileForm, { attachTo: document.body })

    await vi.waitFor(() => {
      const usernameInput = form.findAll('input').find((input) =>
        input.attributes('placeholder')?.includes('用户名'),
      )!
      expect((usernameInput.element as HTMLInputElement).value).toBe('someone')
    })
    expect(getTenantUser).toHaveBeenCalledWith(1)
    wrapper.unmount()
  })

  it('用户名输入框为禁用态(不可编辑)', () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.ProfileForm)
    const usernameInput = form.findAll('input').find((input) =>
      input.attributes('placeholder')?.includes('用户名'),
    )
    expect(usernameInput).toBeTruthy()
    expect((usernameInput!.element as HTMLInputElement).disabled).toBe(true)
    wrapper.unmount()
  })
})

describe('设置页子 tab(个人设置 + 模块管理)', () => {
  it('tabs.ts 声明 profile/menu 子 tab,与页面页签 name 对齐(关于已独立为菜单页)', () => {
    expect(searchTabs.map((item) => item.tab)).toEqual(['profile', 'menu'])
    expect(searchTabs.map((item) => item.label)).toEqual(['个人设置', '模块管理'])
  })
})

describe('config useController 修改密码表单(POST /auth/change-password 对接)', () => {
  it('必填校验:空值提交被拦截并显示反馈,不调接口', async () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.PasswordForm, { attachTo: document.body })

    const submitBtn = form.findAll('button').find((btn) => btn.text().includes('修改密码'))!
    await submitBtn.trigger('click')
    await vi.waitFor(() => {
      expect(form.findAll('.n-form-item-feedback').map((f) => f.text())).toContain('请输入当前密码')
      expect(form.findAll('.n-form-item-feedback').map((f) => f.text())).toContain('请输入新密码')
    })
    // 校验未通过即被拦截,不弹任何确认
    expect(useConfirm).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    expect(changePassword).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('旧密+新密提交:先弹 2 次确认,均确认后调改密接口,toast 后延迟清 token 跳登录', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    const form = mount(controller.PasswordForm, { attachTo: document.body })

    await form.findAll('input').find((input) => input.attributes('placeholder')?.includes('当前密码'))!
      .setValue('old-secret')
    await form.findAll('input').find((input) => input.attributes('placeholder')?.includes('新密码'))!
      .setValue('new-secret-123')
    const submitBtn = form.findAll('button').find((btn) => btn.text().includes('修改密码'))!
    await submitBtn.trigger('click')

    // 2 次确认均通过才发请求
    await vi.waitFor(() => expect(useConfirm).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('密码修改成功,请重新登录'))
    expect(changePassword).toHaveBeenCalledWith({
      old_password: 'old-secret',
      new_password: 'new-secret-123',
    })
    // token 已失效:延迟 1.2s 后才清 token + 整页跳登录(保证 toast 可见)
    expect(authMock.clearToken).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1200)
    expect(authMock.clearToken).toHaveBeenCalledTimes(1)
    expect(goToPortalLogin).toHaveBeenCalledTimes(1)
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('第 1 次确认取消:不发改密请求,不清 token 不跳登录', async () => {
    vi.mocked(useConfirm).mockRejectedValueOnce(new Error('confirm cancelled'))
    const { controller, wrapper } = mountController()
    const form = mount(controller.PasswordForm, { attachTo: document.body })

    await fillPasswordAndSubmit(form)
    await flushPromises()

    expect(useConfirm).toHaveBeenCalledTimes(1)
    expect(changePassword).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    expect(authMock.clearToken).not.toHaveBeenCalled()
    expect(goToPortalLogin).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('第 2 次确认取消:同样不发请求,流程在确认阶段中止', async () => {
    vi.mocked(useConfirm)
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('confirm cancelled'))
    const { controller, wrapper } = mountController()
    const form = mount(controller.PasswordForm, { attachTo: document.body })

    await fillPasswordAndSubmit(form)
    await flushPromises()

    expect(useConfirm).toHaveBeenCalledTimes(2)
    expect(changePassword).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    expect(authMock.clearToken).not.toHaveBeenCalled()
    expect(goToPortalLogin).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('旧密错误(接口 reject):表单保留,不提示成功不跳登录(错误由 HTTP 层提示)', async () => {
    vi.mocked(changePassword).mockRejectedValue(new Error('当前密码错误'))
    const { controller, wrapper } = mountController()
    const form = mount(controller.PasswordForm, { attachTo: document.body })

    await form.findAll('input').find((input) => input.attributes('placeholder')?.includes('当前密码'))!
      .setValue('wrong')
    await form.findAll('input').find((input) => input.attributes('placeholder')?.includes('新密码'))!
      .setValue('new-secret')
    const submitBtn = form.findAll('button').find((btn) => btn.text().includes('修改密码'))!
    await submitBtn.trigger('click')

    await vi.waitFor(() => expect(changePassword).toHaveBeenCalledTimes(1))
    expect(message.success).not.toHaveBeenCalled()
    expect(authMock.clearToken).not.toHaveBeenCalled()
    expect(goToPortalLogin).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
