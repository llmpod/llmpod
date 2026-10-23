import { ref } from 'vue'

/** SSO 配置表单模型 */
export interface SsoModel {
  enabled: boolean
  protocol: string
  issuerUrl: string
  clientId: string
  clientSecret: string
  autoCreateUser: number | null
  defaultRole: string
}

/** LDAP 配置表单模型 */
export interface LdapModel {
  enabled: boolean
  ldapUrl: string
  baseDn: string
}

/** SSO 协议候选(静态) */
export const protocolOptions = [
  { label: 'OIDC', value: 'OIDC' },
  { label: 'SAML 2.0', value: 'SAML' },
  { label: 'CAS', value: 'CAS' },
]

/** 默认角色候选(静态,后续接角色列表接口) */
export const roleOptions = [
  { label: '管理员', value: 'admin' },
  { label: '开发者', value: 'developer' },
  { label: '普通用户', value: 'user' },
]

/** 模块级单例:当前 SSO 配置(静态,后续接配置接口) */
const ssoConfig = ref<SsoModel>({
  enabled: true,
  protocol: 'OIDC',
  issuerUrl: 'https://sso.company.com/auth/realms/ai-platform',
  clientId: 'ai-platform-client',
  clientSecret: 'd8f3a1c2e4b5',
  autoCreateUser: 100,
  defaultRole: 'developer',
})

/** 模块级单例:当前 LDAP 配置(静态,后续接配置接口) */
const ldapConfig = ref<LdapModel>({
  enabled: true,
  ldapUrl: 'ldap://company/com:389',
  baseDn: 'dc=company,dc=com',
})

/** SSO/LDAP 配置 store:读写都走此单例,后续替换为 http 接口 */
export function useStore() {
  /** 保存 SSO 配置(静态:暂无配置接口,仅更新本地副本) */
  const saveSsoConfig = async (model: SsoModel) => {
    ssoConfig.value = { ...model }
  }

  /** 保存 LDAP 配置(静态:暂无配置接口,仅更新本地副本) */
  const saveLdapConfig = async (model: LdapModel) => {
    ldapConfig.value = { ...model }
  }

  return { ssoConfig, ldapConfig, saveSsoConfig, saveLdapConfig }
}
