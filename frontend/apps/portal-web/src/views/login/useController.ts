import { h, ref } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import {
  FormCustom,
  FormInput,
  FormPassword,
  goToPlatform,
  message,
  setToken,
  useForm,
} from '@repo/hooks'
import { isHttpError } from '@repo/http-client'
import { login } from '@/api/auth'

const REMEMBER_KEY = 'portal-login-username'

interface LoginFormModel {
  username: string
  password: string
  remember: boolean
}

export function useController() {
  const remembered = localStorage.getItem(REMEMBER_KEY) ?? ''

  // 维护感知(零接口):首屏横幅读服务端注入的设置种子,被拒后由 catch 分支置位刷新文案
  const maintenanceActive = ref(window.__LLMPOD_SETTINGS__?.maintenanceMode === true)
  const maintenanceMessage = ref(window.__LLMPOD_SETTINGS__?.maintenanceMessage || '系统维护中')

  const { LmForm, submit, submitting } = useForm<LoginFormModel>({
    data: () => ({
      username: remembered,
      password: '',
      remember: Boolean(remembered),
    }),
    options: () => [
      FormInput('', 'username', {
        attrs: { placeholder: '用户名', size: 'large' },
        itemProps: { showLabel: false, class: 'mb-[2rem]' },
        rules: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
      }),
      FormPassword('', 'password', {
        attrs: { placeholder: '密码', size: 'large', showPasswordOn: 'click' },
        itemProps: { showLabel: false, class: 'mb-[1.6rem]' },
        rules: [{ required: true, message: '请输入密码', trigger: 'blur' }],
      }),
      FormCustom(({ model, setValues }) =>
        h('div', { class: 'mb-[2.4rem] flex items-center justify-between' }, [
          h(
            NCheckbox,
            {
              checked: model.remember,
              'onUpdate:checked': (value: boolean) => setValues({ remember: value }),
            },
            { default: () => '记住我' },
          ),
          h(
            NButton,
            { text: true, type: 'primary', onClick: handleForgotPassword },
            { default: () => '忘记密码?' },
          ),
        ]),
      ),
    ],
    submit: async (model, validate) => {
      await validate()
      let result: Awaited<ReturnType<typeof login>>
      try {
        result = await login({
          username: model.username,
          password: model.password,
        })
      } catch (error) {
        // 维护期被拒:按 error_code 分支(勿匹配 message),横幅常显;失败提示已由 http 中间件给出
        if (isHttpError(error) && error.code === 'MAINTENANCE') {
          maintenanceActive.value = true
          maintenanceMessage.value = error.message || '系统维护中'
        }
        return
      }
      setToken(result.platform, result.accessToken)
      // 记住我：仅本地记忆用户名，登录成功后回填
      if (model.remember) localStorage.setItem(REMEMBER_KEY, model.username)
      else localStorage.removeItem(REMEMBER_KEY)
      // 登录后直接跳转账号平台首页，不使用 redirect 回跳
      goToPlatform(result.platform)
    },
  })

  function handleForgotPassword() {
    message.error('请联系管理员重置密码')
  }

  function handleSsoLogin() {
    message.info('企业SSO登录即将上线')
  }

  return { LmForm, submit, submitting, handleSsoLogin, maintenanceActive, maintenanceMessage }
}
