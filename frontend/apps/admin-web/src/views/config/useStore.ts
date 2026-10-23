import type { LlmpodSettings } from '@repo/hooks'
import { useSettingsStore } from '@/store/settings'

/** 通用设置表单模型 */
export interface GeneralModel {
  siteName: string
  siteDescription: string
}

/** 维护设置表单模型 */
export interface MaintenanceModel {
  maintenanceMode: boolean
  maintenanceMessage: string
}

/** 平台配置 store：读写走全局设置接口，与品牌配置页共享同一份回显 */
export function useStore() {
  const settingsStore = useSettingsStore()

  function toGeneralModel(settings: LlmpodSettings | null): GeneralModel {
    return {
      siteName: settings?.siteName ?? '',
      siteDescription: settings?.siteDescription ?? '',
    }
  }

  function toMaintenanceModel(settings: LlmpodSettings | null): MaintenanceModel {
    return {
      maintenanceMode: settings?.maintenanceMode ?? false,
      maintenanceMessage: settings?.maintenanceMessage ?? '',
    }
  }

  /** 保存通用组：站点名(空白回落「LLMPod」不落 null)、描述(空串转 null=清空回落关于页默认副标) */
  function saveGeneral(model: GeneralModel) {
    return settingsStore.save({
      siteName: model.siteName.trim() || 'LLMPod',
      siteDescription: model.siteDescription.trim() || null,
    })
  }

  /** 保存维护组：维护开关 + 维护文案（文案空串转 null = 清空回落默认文案） */
  function saveMaintenance(model: MaintenanceModel) {
    return settingsStore.save({
      maintenanceMode: model.maintenanceMode,
      maintenanceMessage: model.maintenanceMessage || null,
    })
  }

  return { settingsStore, toGeneralModel, toMaintenanceModel, saveGeneral, saveMaintenance }
}
