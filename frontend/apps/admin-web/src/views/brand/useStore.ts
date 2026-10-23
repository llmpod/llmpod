import type { LlmpodSettings } from '@repo/hooks'
import { useSettingsStore } from '@/store/settings'

/** 品牌信息表单模型 */
export interface BrandModel {
  platformName: string
  logo: string
  themeColor: string
  footerText: string
}

/** 登录页定制表单模型 */
export interface LoginPageModel {
  loginPrimary: string
  loginBackground: string
}

/** 品牌配置 store：读写走全局设置接口，与通用设置页共享同一份回显 */
export function useStore() {
  const settingsStore = useSettingsStore()

  /** 回显映射：未配置字段（null）显示空输入框，回落默认由消费方处理 */
  function toBrandModel(settings: LlmpodSettings | null): BrandModel {
    return {
      platformName: settings?.platformName ?? '',
      logo: settings?.logo ?? '',
      themeColor: settings?.themeColor ?? '',
      footerText: settings?.footerText ?? '',
    }
  }

  function toLoginPageModel(settings: LlmpodSettings | null): LoginPageModel {
    return {
      loginPrimary: settings?.loginPrimary ?? '',
      loginBackground: settings?.loginBackground ?? '',
    }
  }

  /** 保存品牌组（三态部分更新：空串转 null = 清空回落默认） */
  function saveBrandInfo(model: BrandModel) {
    return settingsStore.save({
      platformName: model.platformName || null,
      logo: model.logo || null,
      themeColor: model.themeColor || null,
      footerText: model.footerText || null,
    })
  }

  /** 保存登录页组 */
  function saveLoginPageConfig(model: LoginPageModel) {
    return settingsStore.save({
      loginPrimary: model.loginPrimary || null,
      loginBackground: model.loginBackground || null,
    })
  }

  return { settingsStore, toBrandModel, toLoginPageModel, saveBrandInfo, saveLoginPageConfig }
}
