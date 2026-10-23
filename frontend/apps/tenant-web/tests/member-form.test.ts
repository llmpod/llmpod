// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/team', () => ({
  listTenantUsers: vi.fn(),
  createTenantUser: vi.fn(),
  updateTenantUser: vi.fn(),
  deleteTenantUser: vi.fn(),
  getTenantUser: vi.fn(),
}))
vi.mock('@/api/access', () => ({ listRoles: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useDialogClose: vi.fn(),
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import { createTenantUser, updateTenantUser, type TenantUserData } from '@/api/team'
import { listRoles } from '@/api/access'
import { NSelect } from 'naive-ui'
import { message, useDialogClose } from '@repo/hooks'
import MemberForm from '@/views/team/MemberForm.vue'

const closeSpy = vi.fn()

/** 编辑回填用的既有成员 */
const member: TenantUserData = {
  id: 7,
  tenant_id: 1,
  username: 'alice',
  email: 'alice@example.com',
  phone: '13800001111',
  status: 'active',
  last_login_at: null,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-02T00:00:00Z',
  role_ids: [2],
  tenant_balance: '0',
  tenant_frozen_balance: '0',
  tenant_available: '0',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(useDialogClose).mockReturnValue(closeSpy)
  vi.mocked(listRoles).mockResolvedValue([
    { id: 1, name: '管理员' },
    { id: 2, name: '开发者' },
  ] as Awaited<ReturnType<typeof listRoles>>)
})

function mountForm(props: { initial?: TenantUserData } = {}) {
  return mount(MemberForm, { attachTo: document.body, props })
}

/** 调组件 expose 的弹窗确认钩子(useDialog 点「确定」的默认入口) */
function confirm(wrapper: ReturnType<typeof mountForm>, dialogClose = vi.fn()) {
  const vm = wrapper.vm as unknown as { onConfirm: (close: () => void) => Promise<unknown> }
  return { done: vm.onConfirm(dialogClose), dialogClose }
}

/** 按占位文案/类型定位输入框(happy-dom 全量渲染到 body) */
function inputs() {
  return [...document.body.querySelectorAll('input')]
}
function textInput(placeholder: string) {
  return inputs().find((input) => input.placeholder.includes(placeholder))!
}
function setValue(input: HTMLInputElement, value: string) {
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

/** 多选赋值:naive 下拉用虚拟列表,happy-dom 零尺寸渲染 0 项无法点选;
 * 经 NSelect 实例 $emit 触发透传的 update:value 监听器(与点选同路径) */
async function pickRoles(wrapper: ReturnType<typeof mountForm>, roleIds: number[]) {
  const select = wrapper.findComponent(NSelect)
  expect(select.exists(), '角色选择器').toBe(true)
  select.vm.$emit('update:value', roleIds)
  await nextTick()
}

describe('MemberForm 创建模式', () => {
  it('挂载即拉取本租户角色作为选项', async () => {
    const wrapper = mountForm()
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalledTimes(1))
    wrapper.unmount()
  })

  it('必填缺失:onConfirm 被校验拦截,不创建不关弹窗', async () => {
    const wrapper = mountForm()
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    const { done, dialogClose } = confirm(wrapper)
    await done

    expect(createTenantUser).not.toHaveBeenCalled()
    expect(dialogClose).not.toHaveBeenCalled()
    expect(closeSpy).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('非法邮箱被自定义校验拦截', async () => {
    const wrapper = mountForm()
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    setValue(textInput('全局唯一'), 'tester')
    setValue(inputs().find((input) => input.type === 'password')!, 'secret123')
    setValue(textInput('可选'), 'bad-email')
    await pickRoles(wrapper, [1])

    const { done, dialogClose } = confirm(wrapper)
    await done

    expect(createTenantUser).not.toHaveBeenCalled()
    expect(dialogClose).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('完整填写:创建成员(payload 规整),提示并关闭弹窗', async () => {
    vi.mocked(createTenantUser).mockResolvedValue({} as Awaited<ReturnType<typeof createTenantUser>>)
    const wrapper = mountForm()
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    setValue(textInput('全局唯一'), 'tester')
    setValue(inputs().find((input) => input.type === 'password')!, 'secret123')
    await pickRoles(wrapper, [1])

    const { done, dialogClose } = confirm(wrapper)
    await done

    expect(createTenantUser).toHaveBeenCalledWith({
      username: 'tester',
      password: 'secret123',
      role_ids: [1],
      email: undefined,
      phone: undefined,
    })
    expect(updateTenantUser).not.toHaveBeenCalled()
    expect(message.success).toHaveBeenCalledWith('已创建成员:tester')
    expect(dialogClose).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})

describe('MemberForm 编辑模式', () => {
  it('回填:用户名/角色禁用,密码留空占位,邮箱手机号回显', async () => {
    const wrapper = mountForm({ initial: member })
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    const username = textInput('全局唯一')
    expect(username.value).toBe('alice')
    expect(username.disabled).toBe(true)

    const password = textInput('留空则不修改密码')
    expect(password.value).toBe('')

    const optional = inputs().filter(
      (input) => input.placeholder === '可选' && input.type !== 'password',
    )
    expect(optional[0].value).toBe('alice@example.com')
    expect(optional[1].value).toBe('13800001111')

    const select = wrapper.findComponent(NSelect)
    expect(select.props('disabled')).toBe(true)
    wrapper.unmount()
  })

  it('提交:留空密码不改密,payload 仅含资料字段,提示并关闭弹窗', async () => {
    vi.mocked(updateTenantUser).mockResolvedValue(member)
    const wrapper = mountForm({ initial: member })
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    const { done, dialogClose } = confirm(wrapper)
    await done

    expect(updateTenantUser).toHaveBeenCalledWith(member.id, {
      password: undefined,
      email: 'alice@example.com',
      phone: '13800001111',
    })
    expect(createTenantUser).not.toHaveBeenCalled()
    expect(message.success).toHaveBeenCalledWith('已保存:alice')
    expect(dialogClose).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('提交:填写密码时随 payload 一并重置', async () => {
    vi.mocked(updateTenantUser).mockResolvedValue(member)
    const wrapper = mountForm({ initial: member })
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    setValue(textInput('留空则不修改密码'), 'newpass456')

    const { done } = confirm(wrapper)
    await done

    expect(updateTenantUser).toHaveBeenCalledWith(member.id, {
      password: 'newpass456',
      email: 'alice@example.com',
      phone: '13800001111',
    })
    wrapper.unmount()
  })

  it('非法邮箱被自定义校验拦截,不发起更新不关弹窗', async () => {
    const wrapper = mountForm({ initial: member })
    await vi.waitFor(() => expect(listRoles).toHaveBeenCalled())

    setValue(textInput('可选'), 'bad-email')

    const { done, dialogClose } = confirm(wrapper)
    await done

    expect(updateTenantUser).not.toHaveBeenCalled()
    expect(dialogClose).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
