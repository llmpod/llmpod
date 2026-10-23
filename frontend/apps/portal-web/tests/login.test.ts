// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ login: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    goToPlatform: vi.fn(),
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
  }
})

import { login } from '@/api/auth'
import { NCheckbox } from 'naive-ui'
import { HttpError } from '@repo/http-client'
import { goToPlatform, message } from '@repo/hooks'
import { useController } from '@/views/login/useController'

function mountLogin() {
  let controller!: ReturnType<typeof useController>
  mount({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  const form = mount(controller.LmForm, { attachTo: document.body })
  return { controller, form }
}

async function fillCredentials(username: string, password: string, form: ReturnType<typeof mount>) {
  const usernameInput = form.findAll('input').find((input) =>
    input.attributes('placeholder') === '用户名',
  )!
  const passwordInput = form.findAll('input').find((input) => input.attributes('type') === 'password')!
  await usernameInput.setValue(username)
  await passwordInput.setValue(password)
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  delete window.__LLMPOD_SETTINGS__
})

describe('portal login 记住我', () => {
  it('已记忆用户名:挂载时回填用户名并勾选记住我', () => {
    localStorage.setItem('portal-login-username', 'remembered-user')
    const { form } = mountLogin()
    const usernameInput = form.findAll('input').find((input) =>
      input.attributes('placeholder') === '用户名',
    )!
    expect((usernameInput.element as HTMLInputElement).value).toBe('remembered-user')
    // 勾选态经 NCheckbox checked 传入
    expect(form.findComponent(NCheckbox).props('checked')).toBe(true)
  })

  it('未记忆:用户名为空且不勾选', () => {
    const { form } = mountLogin()
    const usernameInput = form.findAll('input').find((input) =>
      input.attributes('placeholder') === '用户名',
    )!
    expect((usernameInput.element as HTMLInputElement).value).toBe('')
    expect(form.findComponent(NCheckbox).props('checked')).toBe(false)
  })
})

describe('portal login 提交流程', () => {
  it('登录成功:access_domain=platform 落 admin token 并整页跳转', async () => {
    vi.mocked(login).mockResolvedValue({ platform: 'admin', accessToken: 'tok-admin' })
    const { controller, form } = mountLogin()
    await fillCredentials('admin', 'secret', form)

    await controller.submit()

    expect(login).toHaveBeenCalledWith({ username: 'admin', password: 'secret' })
    expect(localStorage.getItem('admin_access_token')).toBe('tok-admin')
    expect(goToPlatform).toHaveBeenCalledWith('admin')
  })

  it('access_domain=tenant 落 tenant token 并跳租户端', async () => {
    vi.mocked(login).mockResolvedValue({ platform: 'tenant', accessToken: 'tok-tenant' })
    const { controller, form } = mountLogin()
    await fillCredentials('user', 'secret', form)

    await controller.submit()

    expect(localStorage.getItem('tenant_access_token')).toBe('tok-tenant')
    expect(goToPlatform).toHaveBeenCalledWith('tenant')
  })

  it('勾选记住我:登录成功写入用户名;未勾选清除记忆', async () => {
    vi.mocked(login).mockResolvedValue({ platform: 'admin', accessToken: 'tok' })
    const { controller, form } = mountLogin()
    await fillCredentials('zhangsan', 'secret', form)

    // 勾选记住我
    form.findComponent(NCheckbox).vm.$emit('update:checked', true)
    await controller.submit()
    expect(localStorage.getItem('portal-login-username')).toBe('zhangsan')

    // 未勾选:清除记忆
    localStorage.clear()
    vi.clearAllMocks()
    const { controller: c2, form: f2 } = mountLogin()
    await fillCredentials('lisi', 'secret', f2)
    await c2.submit()
    expect(localStorage.getItem('portal-login-username')).toBeNull()
  })

  it('必填缺失:校验拦截不调登录接口不跳转', async () => {
    const { controller } = mountLogin()
    await expect(controller.submit()).rejects.toBeTruthy()
    expect(login).not.toHaveBeenCalled()
    expect(goToPlatform).not.toHaveBeenCalled()
  })

  it('忘记密码与 SSO 提示', () => {
    const { controller } = mountLogin()
    controller.handleSsoLogin()
    expect(message.info).toHaveBeenCalledWith('企业SSO登录即将上线')
  })
})

describe('portal login 维护感知', () => {
  it('注入种子维护开启:挂载即横幅常显,文案回落默认', () => {
    window.__LLMPOD_SETTINGS__ = {
      siteName: null,
      siteDescription: null,
      platformName: null,
      logo: null,
      footerText: null,
      themeColor: null,
      mode: null,
      loginBackground: null,
      loginPrimary: null,
      maintenanceMode: true,
      maintenanceMessage: null,
    }
    const { controller } = mountLogin()

    expect(controller.maintenanceActive.value).toBe(true)
    expect(controller.maintenanceMessage.value).toBe('系统维护中')
  })

  it('登录被拒 MAINTENANCE:横幅置位并采用后端文案,不跳转', async () => {
    vi.mocked(login).mockRejectedValue(
      new HttpError({
        type: 'http',
        message: '数据库升级,预计 30 分钟',
        code: 'MAINTENANCE',
        status: 503,
      }),
    )
    const { controller, form } = mountLogin()
    await fillCredentials('user', 'secret', form)

    // 维护被拒走横幅分支,错误不向上抛(提示已由 http 中间件给出)
    await expect(controller.submit()).resolves.toBeUndefined()

    expect(controller.maintenanceActive.value).toBe(true)
    expect(controller.maintenanceMessage.value).toBe('数据库升级,预计 30 分钟')
    expect(goToPlatform).not.toHaveBeenCalled()
  })

  it('非维护错误(密码错):不置维护横幅', async () => {
    vi.mocked(login).mockRejectedValue(
      new HttpError({ type: 'http', message: '用户名或密码错误', status: 401 }),
    )
    const { controller, form } = mountLogin()
    await fillCredentials('user', 'wrong', form)

    await expect(controller.submit()).resolves.toBeUndefined()

    expect(controller.maintenanceActive.value).toBe(false)
    expect(goToPlatform).not.toHaveBeenCalled()
  })
})
