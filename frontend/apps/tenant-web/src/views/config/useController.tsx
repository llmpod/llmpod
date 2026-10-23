import {
  useForm,
  FormInput,
  FormPassword,
  FormButton,
  message,
  useConfirm,
  goToPortalLogin,
} from '@repo/hooks'
import { useAuthStore } from '@/store/auth'
import { getTenantUser, updateTenantUser } from '@/api/team'
import { changePassword } from '@/api/auth'

interface ProfileFormValues {
  username: string
  email: string
  phone: string
}

interface PasswordFormValues {
  old_password: string
  new_password: string
}

/** 改密成功后滞留时长:先让 toast 可见,再清 token 整页跳登录 */
const REDIRECT_DELAY_MS = 1200

export function useController() {
  const auth = useAuthStore()

  /** 当前用户 ID(登录后由 auth store 持有);资料/改密接口都按它寻址 */
  const userId = () => auth.userInfo?.user_id

  // 个人信息表单(回填走 getTenantUser;username 不可改,仅展示)
  const {
    LmForm: ProfileForm,
    params: profileParams,
  } = useForm<ProfileFormValues>({
    data: () => ({
      username: '',
      email: '',
      phone: '',
    }),
    options: () => [
      FormInput('用户名', 'username', {
        attrs: { placeholder: '用户名', disabled: true },
      }),
      FormInput('邮箱', 'email', {
        rules: [
          {
            pattern: /^[\w.-]+@[\w-]+(\.[\w-]+)+$/,
            message: '请输入正确的邮箱格式',
            trigger: 'blur',
          },
        ],
        attrs: { placeholder: '请输入邮箱' },
      }),
      FormInput('手机号', 'phone', {
        attrs: { placeholder: '请输入手机号' },
      }),
      FormButton('保存修改', {
        attrs: { type: 'primary' },
        onClick: ({ submit }) => submit().catch(() => {}),
      }),
    ],
    layout: { labelPlacement: 'top' },
    submit: async (model, validate) => {
      await validate()
      const id = userId()
      if (!id) {
        message.error('用户信息未加载,请重新登录')
        return
      }
      await updateTenantUser(id, { email: model.email, phone: model.phone })
      message.success('个人信息已保存')
    },
  })

  // 修改密码表单(自助改密:验旧密 → 落新密;成功后当前 token 立即失效,跳登录页)
  const {
    LmForm: PasswordForm,
    params: passwordParams,
  } = useForm<PasswordFormValues>({
    data: () => ({
      old_password: '',
      new_password: '',
    }),
    options: () => [
      FormPassword('当前密码', 'old_password', {
        rules: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
        attrs: { placeholder: '输入当前密码', showPasswordOn: 'click' },
      }),
      FormPassword('新密码', 'new_password', {
        rules: [{ required: true, message: '请输入新密码', trigger: 'blur' }],
        attrs: { placeholder: '输入新密码', showPasswordOn: 'click' },
      }),
      FormButton('修改密码', {
        attrs: { type: 'primary' },
        onClick: ({ submit }) => submit().catch(() => {}),
      }),
    ],
    layout: { labelPlacement: 'top' },
    submit: async (model, validate) => {
      await validate()
      // 改密是高危操作(成功即踢下线重登):连续 2 次确认,任一取消即中止不发请求
      const confirmSteps = [
        {
          title: '确认修改密码',
          content: '修改成功后当前登录将立即失效,需使用新密码重新登录,确定继续吗?',
          confirmText: '继续',
          cancelText: '取消',
        },
        {
          title: '最终确认',
          content: '请再次确认执行本次密码修改。',
          confirmText: '确认修改',
          cancelText: '返回',
        },
      ]
      for (const step of confirmSteps) {
        try {
          await useConfirm(step)
        } catch {
          // 取消/关闭/Esc:流程已打断
          return
        }
      }
      // 旧密错误由 HTTP 层提示(401 走普通错误反馈),表单保留
      await changePassword({
        old_password: model.old_password,
        new_password: model.new_password,
      })
      message.success('密码修改成功,请重新登录')
      Object.assign(passwordParams(), { old_password: '', new_password: '' })
      // 成功后当前 token 立即失效:先 toast 再清 token 跳登录
      setTimeout(() => {
        auth.clearToken()
        goToPortalLogin()
      }, REDIRECT_DELAY_MS)
    },
  })

  // 回填个人资料(email/phone 不在 auth store 的 UserInfoData 内,须取成员详情)
  onMounted(async () => {
    const id = userId()
    if (!id) return
    try {
      const me = await getTenantUser(id)
      Object.assign(profileParams(), {
        username: me.username,
        email: me.email ?? '',
        phone: me.phone ?? '',
      })
    } catch {
      // 失败由 http 层统一提示,表单保持可编辑
    }
  })

  return { ProfileForm, PasswordForm }
}
