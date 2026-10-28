import type { LlmpodSettings } from '@repo/hooks'
import { request } from './request'

/**
 * 管理端回显数据（GET /api/v1/admin/settings）：
 * 全局设置 12 字段 + 更新元数据，不走 TTL 缓存，回显即库中最新值。
 */
export interface AdminSettingsData extends LlmpodSettings {
  /** 最后更新人用户 ID（可空） */
  updated_by?: number | null
  /** 最后更新时间（可空） */
  updated_at?: string | null
}

/**
 * 部分更新请求体（POST /api/v1/admin/settings/update）：
 * 键缺省 = 不动；null / 空串 = 清空；有值 = 覆写。
 * 非法值后端返回 422（error_code: VALIDATION_ERROR），库中原值不受影响，修正重发即可。
 */
export type SettingsUpdatePayload = Partial<LlmpodSettings>

/** 获取全局设置-管理端回显（GET /api/v1/admin/settings） */
export function getAdminSettings(): Promise<AdminSettingsData> {
  return request({ url: '/admin/settings', method: 'GET' })
}

/** 部分更新全局设置（POST /api/v1/admin/settings/update），响应 data = 合并后全量（12 字段） */
export function updateAdminSettings(data: SettingsUpdatePayload): Promise<LlmpodSettings> {
  return request({ url: '/admin/settings/update', method: 'POST', data })
}
