import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getAdminSettings, updateAdminSettings, type AdminSettingsData } from '@/api/settings'

/**
 * 全局设置共享 store：设置页通用/品牌两个子 tab 与后续消费方共用一份回显。
 * settings 为 null 表示尚未加载（表单回落空输入框）；加载失败提示由 http 中间件统一给出。
 */
export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AdminSettingsData | null>(null)
  const loading = ref(false)

  /** 拉取管理端回显；已加载且未强制时跳过，页面间切换不重复请求 */
  async function load(force = false): Promise<void> {
    if (loading.value || (settings.value && !force)) return
    loading.value = true
    try {
      settings.value = await getAdminSettings()
    } finally {
      loading.value = false
    }
  }

  /** 部分更新并把合并后全量写回单例；随后静默刷新一次回显以同步 updated_by/updated_at */
  async function save(payload: Parameters<typeof updateAdminSettings>[0]) {
    const merged = await updateAdminSettings(payload)
    settings.value = { ...settings.value, ...merged }
    await load(true).catch(() => {})
    return merged
  }

  return { settings, loading, load, save }
})
