import type { LlmpodSettings } from '@repo/hooks'
import type { SettingsUpdatePayload } from '@/api/settings'
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

/** body 块轮转方式（后端枚举：size=按容量 / time=按时间，非法值 422） */
export type BodyRetentionMode = 'size' | 'time'

/** 调用日志设置表单模型 */
export interface BodyLogModel {
  /** 记录请求体快照（默认关闭） */
  bodySnapshot: boolean
  /** 轮转方式：回显 null 时页面按「按时间」展示，用户未切换则不回写 */
  bodyRetentionMode: BodyRetentionMode
  /** 保留天数（仅 time 模式消费）；null = 用系统默认 */
  bodyRetentionDays: number | null
  /** 容量上限，GiB（仅 size 模式消费）；null = 用系统默认，提交时换算字节 */
  bodyMaxBytes: number | null
}

/** 容量下限：1 MiB（后端低于即 422） */
export const MIN_BODY_MAX_BYTES = 1024 * 1024

/** 字节 / GiB 换算基数 */
const BYTES_PER_GIB = 1024 ** 3

/** 字节 → GiB（一位小数，与输入框精度一致，非整 GiB 回显不会被二次改写） */
export function bytesToGib(bytes: number): number {
  return Number((bytes / BYTES_PER_GIB).toFixed(1))
}

/** GiB → 字节（后端要求字节整数，按约定四舍五入） */
export function gibToBytes(gib: number): number {
  return Math.round(gib * BYTES_PER_GIB)
}

/** 容量上限是否合法：null（用系统默认）或 ≥ 1 MiB */
export function isValidBodyMaxGib(gib: number | null): boolean {
  return gib == null || gib * BYTES_PER_GIB >= MIN_BODY_MAX_BYTES
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

  /**
   * 调用日志组回显：mode 为 null 时归一成「按时间」供 radio 展示，
   * 容量按字节回显、页面以 GiB 呈现（null 保持 null = 用系统默认）。
   */
  function toBodyLogModel(settings: LlmpodSettings | null): BodyLogModel {
    const maxBytes = settings?.bodyMaxBytes
    return {
      bodySnapshot: settings?.bodySnapshot ?? false,
      bodyRetentionMode: settings?.bodyRetentionMode ?? 'time',
      bodyRetentionDays: settings?.bodyRetentionDays ?? null,
      bodyMaxBytes: maxBytes == null ? null : bytesToGib(maxBytes),
    }
  }

  /** 遥测开关回显：缺省 true（旧缓存种子/旧版后端不带该键时按默认开启） */
  function resolveTelemetry(settings: LlmpodSettings | null): boolean {
    return settings?.telemetryEnabled ?? true
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

  /** 保存遥测开关：只提交开关一键，其余设置字段保持后端原值不动（开关即改即存，无保存按钮） */
  function saveTelemetry(telemetryEnabled: boolean) {
    return settingsStore.save({ telemetryEnabled })
  }

  /**
   * 调用日志组部分更新载荷：只带用户实际改动的键（键缺省 = 后端不动 / null = 清空回落默认）。
   * 回显 bodyRetentionMode 为 null 时页面按「按时间」展示，未切 radio 不回写，
   * 避免仅改开关或数值就把「未配置」固化成某个模式。无改动返回 null。
   */
  function diffBodyLog(model: BodyLogModel): SettingsUpdatePayload | null {
    const base = toBodyLogModel(settingsStore.settings)
    const payload: SettingsUpdatePayload = {}
    if (model.bodySnapshot !== base.bodySnapshot) payload.bodySnapshot = model.bodySnapshot
    if (model.bodyRetentionMode !== base.bodyRetentionMode) {
      payload.bodyRetentionMode = model.bodyRetentionMode
    }
    if (model.bodyRetentionDays !== base.bodyRetentionDays) {
      payload.bodyRetentionDays = model.bodyRetentionDays
    }
    if (model.bodyMaxBytes !== base.bodyMaxBytes) {
      payload.bodyMaxBytes = model.bodyMaxBytes == null ? null : gibToBytes(model.bodyMaxBytes)
    }
    return Object.keys(payload).length > 0 ? payload : null
  }

  /** 保存调用日志组：无改动返回 null（调用方提示即可），有改动才发一次部分更新 */
  function saveBodyLog(model: BodyLogModel) {
    const payload = diffBodyLog(model)
    return payload ? settingsStore.save(payload) : null
  }

  return {
    settingsStore,
    toGeneralModel,
    toMaintenanceModel,
    toBodyLogModel,
    resolveTelemetry,
    saveGeneral,
    saveMaintenance,
    saveTelemetry,
    diffBodyLog,
    saveBodyLog,
  }
}
