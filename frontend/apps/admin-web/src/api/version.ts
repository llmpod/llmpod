import { request } from './request'

/**
 * 更新检查响应数据（GET /api/v1/version/latest，需登录）。
 * calver（yy.m.d[.n]）比较语义后端已处理——红点唯一判定依据是 has_update，
 * 前端绝不自己比版本字符串；latest_version 恒 ≥ current_version，判等安全。
 */
export interface VersionLatestData {
  /** 当前平台运行版本 */
  current_version: string
  /** 最新版本；无更新/检查禁用/上游失败三种情形下 == current_version */
  latest_version: string
  /** 红点 / 「有新版本」横幅唯一判定依据 */
  has_update: boolean
  /** 更新检查是否启用；false = 官网 manifest 未上线，恒无更新 */
  update_check_enabled: boolean
  /** 最近一次成功检查时间（ISO 8601）；从未成功为 null */
  checked_at: string | null
  /** 新版本更新说明，支持简单 HTML 片段（a/ul/code 等排版标签）；渲染前必须经 DOMPurify 白名单消毒 */
  notes: string | null
  /** 更新详情 / 下载页链接 */
  release_url: string | null
  /** 新版本发布时间（ISO 8601） */
  published_at: string | null
  /** 上游不可达等错误的展示文案；null = 正常 */
  error_message: string | null
}

/**
 * 检查更新（GET /api/v1/version/latest）。
 * 后端有进程级缓存（最快 5 分钟一次真实外呼），前端无需自行节流；
 * 首次未命中可能慢到 5s，勿阻塞首屏渲染。
 */
export function checkUpdate(signal?: AbortSignal): Promise<VersionLatestData> {
  return request({ url: '/version/latest', method: 'GET', signal })
}

/**
 * 注入变量携带的平台版本（只出现在 window.__LLMPOD_SETTINGS__，settings API 响应不含）；
 * 管理员未配置任何设置时整个种子为 null，取不到返回 undefined，
 * 兜底走 checkUpdate 的 current_version。
 */
export function getServerVersion(): string | undefined {
  return window.__LLMPOD_SETTINGS__?.serverVersion ?? undefined
}
