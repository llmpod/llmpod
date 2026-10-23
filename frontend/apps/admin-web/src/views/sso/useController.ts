import {
  useForm,
  FormInput,
  FormPassword,
  FormInputNumber,
  FormSelect,
  FormSwitch,
  FormButton,
  message,
} from '@repo/hooks'
import {
  useStore,
  protocolOptions,
  roleOptions,
  type SsoModel,
  type LdapModel,
} from './useStore'

export function useController() {
  const { ssoConfig, ldapConfig, saveSsoConfig, saveLdapConfig } = useStore()

  const { LmForm: SsoForm } = useForm<SsoModel>({
    data: () => ({ ...ssoConfig.value }),
    options: () => [
      FormSwitch('启用SSO', 'enabled', {
        itemProps: { feedback: '开启后用户可通过 SSO 登录' },
      }),
      FormSelect('SSO协议', 'protocol', protocolOptions, {
        rules: [{ required: true, message: '请选择SSO协议', trigger: 'change' }],
        attrs: { class: 'max-w-[440px]' },
        itemProps: { class: 'mt-[1rem]' },
      }),
      FormInput('Issuer URL', 'issuerUrl', {
        rules: [{ required: true, message: '请输入Issuer URL', trigger: 'blur' }],
        attrs: { class: 'max-w-[440px]', placeholder: 'https://sso.company.com/auth/realms/xxx' },
      }),
      FormInput('Client ID', 'clientId', {
        rules: [{ required: true, message: '请输入Client ID', trigger: 'blur' }],
        attrs: { class: 'max-w-[440px]' },
      }),
      FormPassword('Client Secret', 'clientSecret', {
        rules: [{ required: true, message: '请输入Client Secret', trigger: 'blur' }],
        attrs: { class: 'max-w-[440px]' },
      }),
      FormInputNumber('自动创建用户', 'autoCreateUser', {
        attrs: { min: 0, class: 'w-[220px]' },
      }),
      FormSelect('默认角色', 'defaultRole', roleOptions, {
        attrs: { class: 'max-w-[440px]' },
      }),
      FormButton('保存配置', {
        attrs: { type: 'primary', secondary: false },
        // 校验失败 reject 属于预期路径,吞掉避免未处理拒绝(同 tenant config)
        onClick: (ctx) => ctx.submit().catch(() => {}),
      }),
    ],
    submit: async (model, validate) => {
      await validate()
      await saveSsoConfig(model)
      message.success('配置已保存')
    },
  })

  const { LmForm: LdapForm } = useForm<LdapModel>({
    data: () => ({ ...ldapConfig.value }),
    options: () => [
      FormSwitch('启用LDAP', 'enabled', {
        itemProps: { feedback: '同步企业 AD 目录' },
      }),
      FormInput('LDAP URL', 'ldapUrl', {
        rules: [{ required: true, message: '请输入LDAP URL', trigger: 'blur' }],
        attrs: { class: 'max-w-[440px]', placeholder: 'ldap://host:389' },
        itemProps: { class: 'mt-[1rem]' },
      }),
      FormInput('Base DN', 'baseDn', {
        rules: [{ required: true, message: '请输入Base DN', trigger: 'blur' }],
        attrs: { class: 'max-w-[440px]', placeholder: 'dc=company,dc=com' },
      }),
      FormButton('保存', {
        attrs: { type: 'primary', secondary: false },
        // 校验失败 reject 属于预期路径,吞掉避免未处理拒绝(同 tenant config)
        onClick: (ctx) => ctx.submit().catch(() => {}),
      }),
    ],
    submit: async (model, validate) => {
      await validate()
      await saveLdapConfig(model)
      message.success('配置已保存')
    },
  })

  return { SsoForm, LdapForm }
}
