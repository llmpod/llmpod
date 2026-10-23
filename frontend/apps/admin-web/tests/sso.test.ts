// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, message: { success: vi.fn(), error: vi.fn() } }
})

import { message } from '@repo/hooks'
import { protocolOptions, roleOptions, useStore } from '@/views/sso/useStore'
import { useController } from '@/views/sso/useController'

const store = useStore()

beforeEach(() => vi.clearAllMocks())

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

describe('sso useStore', () => {
  it('saveSsoConfig/saveLdapConfig 覆盖写入本地配置', async () => {
    await store.saveSsoConfig({
      enabled: false,
      protocol: 'SAML',
      issuerUrl: 'https://new-sso.example.com',
      clientId: 'new-client',
      clientSecret: 'secret',
      autoCreateUser: null,
      defaultRole: 'user',
    })
    expect(store.ssoConfig.value).toMatchObject({ enabled: false, protocol: 'SAML', clientId: 'new-client' })

    await store.saveLdapConfig({ enabled: false, ldapUrl: 'ldap://new:389', baseDn: 'dc=new,dc=com' })
    expect(store.ldapConfig.value).toMatchObject({ enabled: false, ldapUrl: 'ldap://new:389' })
  })

  it('协议与角色选项常量', () => {
    expect(protocolOptions.map((item) => item.value)).toEqual(['OIDC', 'SAML', 'CAS'])
    expect(roleOptions.length).toBeGreaterThan(0)
  })
})

describe('sso useController', () => {
  it('SSO 表单必填校验:清空 Issuer URL 提交被拦截不提示', async () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.SsoForm, { attachTo: document.body })

    const issuerInput = form.findAll('input').find((input) =>
      input.attributes('placeholder')?.includes('sso.company.com'),
    )!
    await issuerInput.setValue('')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => {
      expect(form.findAll('.n-form-item-feedback').length).toBeGreaterThan(0)
    })
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('SSO 表单初始值合法:提交保存配置并提示', async () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.SsoForm, { attachTo: document.body })

    const before = { ...store.ssoConfig.value }
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))
    expect(store.ssoConfig.value.protocol).toBe(before.protocol)
    wrapper.unmount()
  })

  it('LDAP 表单提交保存并提示', async () => {
    const { controller, wrapper } = mountController()
    const form = mount(controller.LdapForm, { attachTo: document.body })

    const saveBtn = form.findAll('button').find((btn) => btn.text() === '保存')!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))
    wrapper.unmount()
  })
})
