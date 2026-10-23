import { computed, ref } from 'vue'
import { useStorage } from '@vueuse/core'
import { message } from '../components/message'

/**
 * 检查更新接口返回数据（GET /api/v1/version/latest 的 data，字段名与后端对齐）。
 * calver 比较语义后端已处理——红点唯一判定依据是 has_update，前端绝不自己比版本字符串。
 */
export interface VersionCheckResult {
  /** 当前平台运行版本 */
  current_version: string
  /** 最新版本；恒 ≥ current，无更新/检查禁用/上游失败三种情形下 == current（判等安全） */
  latest_version: string
  /** 红点 / 「有新版本」横幅唯一判定依据（涵盖无更新 / 检查禁用 / 上游失败均为 false） */
  has_update: boolean
  /** 更新检查是否启用；false = 官网 manifest 未上线，恒无更新，不渲染红点、不提示错误 */
  update_check_enabled?: boolean
  /** 最近一次成功检查时间（ISO 8601）；从未成功为 null */
  checked_at?: string | null
  /** 新版本更新说明，支持简单 HTML 片段（a/ul/code 等排版标签）；渲染前必须经 DOMPurify 白名单消毒 */
  notes?: string | null
  /** 更新详情 / 下载页链接 */
  release_url?: string | null
  /** 新版本发布时间（ISO 8601） */
  published_at?: string | null
  /** 上游不可达等错误的展示文案；null = 正常 */
  error_message?: string | null
}

export interface UseVersionCheckOptions {
  /** 已查看最新版本的 localStorage key（按平台前缀隔离，如 llmpod:tenant:update-viewed） */
  storageKey: string
  /** 检查更新接口（返回 /version/latest 的 data） */
  fetchLatest: () => Promise<VersionCheckResult>
}

/**
 * 版本更新检查（仅管理端使用）：
 * - hasUpdate（红点）= 接口 has_update === true 且该最新版本未被用户查看过
 *   （viewedVersion 仅做「是否已查看」的字符串等值判断，不做版本大小比较）。
 * - hasNewVersion（更新内容展示）= 接口 has_update === true，与是否已查看无关：
 *   关于页每次检查/进入页面，只要上游报告有新版本就渲染更新说明面板。
 * 手动检查会标记该版本为已查看（红点消失，持久化到 localStorage），并按
 * 上游结果分支提示：有更新 → 发现新版本（即使之前已查看过）；error_message 非空 →
 * 轻提示（不弹错误弹窗）；其余（无更新 / 检查禁用）→ 当前为最新版本。
 * manualCheck 返回最近一次检查结果（网络失败为 null），调用方可据此决定是否跳转关于页。
 * silentCheck 静默吞错（不阻塞首屏、不打扰用户），manualCheck 内部兜底网络错误提示。
 */
export function useVersionCheck(options: UseVersionCheckOptions) {
  /** 已查看过的最新版本号 */
  const viewedVersion = useStorage<string>(options.storageKey, '')
  /** 最近一次检查的完整结果（null = 尚未检查成功） */
  const updateInfo = ref<VersionCheckResult | null>(null)
  /** 当前平台运行版本（来自接口 current_version；展示侧应优先注入变量 serverVersion） */
  const currentVersion = ref('')
  /** 最近一次检查到的最新版本号 */
  const latestVersion = ref('')

  /** 有未查看的新版本 → 头像红点 */
  const hasUpdate = computed(() => {
    const info = updateInfo.value
    return !!info && info.has_update && info.latest_version !== viewedVersion.value
  })

  /** 上游报告有新版本（与是否已查看无关）→ 关于页更新内容展示 */
  const hasNewVersion = computed(() => !!updateInfo.value?.has_update)

  /** 拉取并落状态；调用方对失败自行分支（silentCheck 吞错 / manualCheck 提示） */
  async function fetch(): Promise<void> {
    const info = await options.fetchLatest()
    updateInfo.value = info
    currentVersion.value = info.current_version
    latestVersion.value = info.latest_version
  }

  /** 静默检查：只更新红点状态，不弹提示、吞一切错误（进入页面时调用，勿阻塞关键路径） */
  async function silentCheck(): Promise<void> {
    await fetch().catch(() => {})
  }

  /**
   * 手动检查（点击「检查更新」）：按上游结果分支提示并标记已查看。
   * 有更新时即使已查看过也提示「发现新版本」——展示侧（关于页面板）每次都渲染。
   * 返回最近一次检查结果，网络失败返回 null（此时已弹兜底提示）。
   */
  async function manualCheck(): Promise<VersionCheckResult | null> {
    try {
      await fetch()
    } catch {
      message.warning('检查更新失败,请稍后重试')
      return null
    }
    const info = updateInfo.value
    if (!info) return null
    if (info.error_message) {
      // 上游不可达等：轻提示，不弹错误弹窗（红点恒不亮，has_update 为 false）
      message.warning(info.error_message)
      return info
    }
    if (info.has_update) {
      viewedVersion.value = info.latest_version
      message.success(`发现新版本 v${info.latest_version}`)
    } else {
      // 涵盖无更新 / 检查禁用（红点恒不亮，视为已知版本）
      message.info('当前为最新版本')
    }
    return info
  }

  return { hasUpdate, hasNewVersion, currentVersion, latestVersion, updateInfo, silentCheck, manualCheck }
}
