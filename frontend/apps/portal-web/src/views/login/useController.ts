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
/** 登录表单 id:表单外的登录按钮用 form 属性挂回 <form>,浏览器据此识别为「带提交按钮的登录表单」才会存密码 */
const LOGIN_FORM_ID = 'portal-login-form'

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
    // novalidate:必填校验统一由下方 rules 给出,避免 Enter 隐式提交时再叠一层浏览器原生气泡
    formProps: { id: LOGIN_FORM_ID, novalidate: true },
    options: () => [
      FormInput('', 'username', {
        attrs: {
          placeholder: '用户名',
          size: 'large',
          // inputProps 才透传到内层 <input>:浏览器/密码管理器靠 autocomplete + 稳定 name 建立「账号」语义,
          // 写在外层 attrs 上只会落到 n-input 的包裹 div(不参与自动填充识别)
          inputProps: {
            name: 'username',
            autocomplete: 'username',
            required: true,
            spellcheck: 'false',
            autocapitalize: 'off',
            enterkeyhint: 'next',
          },
        },
        itemProps: { showLabel: false, class: 'mb-[2rem]' },
        rules: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
      }),
      FormPassword('', 'password', {
        attrs: {
          placeholder: '密码',
          size: 'large',
          showPasswordOn: 'click',
          // current-password:声明「已有的密码」,浏览器才会带出已保存的凭据并提示保存
          inputProps: {
            name: 'password',
            autocomplete: 'current-password',
            required: true,
            enterkeyhint: 'done',
          },
        },
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

  return {
    LmForm,
    submit,
    submitting,
    formId: LOGIN_FORM_ID,
    handleSsoLogin,
    maintenanceActive,
    maintenanceMessage,
  }
}
