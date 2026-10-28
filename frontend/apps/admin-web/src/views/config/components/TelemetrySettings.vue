<script setup lang="ts">
import { HelpCircleOutline } from '@vicons/ionicons5'
import { message, useConfirm } from '@repo/hooks'
import { useStore } from '../useStore'

/**
 * 用户体验改进计划：单开关，切换即保存（无保存按钮）。
 * 卡片内容自治：不参与通用/维护表单的校验与提交链路，
 * 回显取全局设置单例（未下发该键时缺省开启）。
 */
const { settingsStore, resolveTelemetry, saveTelemetry } = useStore()

/** 计划说明文档地址：外链交给浏览器新窗口打开，不再用内嵌弹窗承载 */
const NOTICE_URL = 'https://docs.llmpod.ai/telemetry'

/** 开关值：先乐观更新再等接口，失败回滚到切换前状态 */
const enabled = ref(resolveTelemetry(settingsStore.settings))
/** 提交中：切换期间禁用，避免连点产生并发写 */
const saving = ref(false)
/** 二次确认弹窗展示中：与提交中一并锁开关，避免叠开多个确认框 */
const confirming = ref(false)

/** 开关交互锁定（确认中或提交中） */
const busy = computed(() => confirming.value || saving.value)

/** 回显到达后同步（含其它卡片保存后单例刷新） */
watch(
  () => settingsStore.settings,
  (settings) => {
    if (settings) enabled.value = resolveTelemetry(settings)
  },
)

/** 首次进入自行拉取回显（load 幂等：已加载且非强制时跳过） */
void settingsStore.load()

async function onChange(value: boolean) {
  if (busy.value) return
  // 退出需二次确认：关闭后不再上报诊断数据，属用户可感知的行为变更，避免误触
  if (!value) {
    confirming.value = true
    try {
      await useConfirm({
        title: '退出用户体验改进计划',
        content: '退出后将停止向我们发送匿名的诊断数据和使用情况统计。确认退出?',
        confirmText: '退出',
        cancelText: '取消',
      })
    } catch {
      // 取消/关闭确认框：开关保持开启，不发请求
      return
    } finally {
      confirming.value = false
    }
  }
  const previous = enabled.value
  enabled.value = value
  saving.value = true
  try {
    await saveTelemetry(value)
    message.success(value ? '已加入用户体验改进计划' : '已退出用户体验改进计划')
  } catch {
    // 接口异常已由 HTTP 层统一提示，这里只把开关拨回原状态，避免界面与后端不一致
    enabled.value = previous
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="flex items-center gap-[1.6rem] pt-[.5rem]">
    <div class="flex items-center gap-[.6rem]">
      <span class="text-default">用户体验改进计划</span>
      <a
        :href="NOTICE_URL"
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex text-xl text-primary transition-opacity hover:opacity-[0.75]"
        title="查看计划说明"
        aria-label="用户体验改进计划说明"
      >
        <n-icon :component="HelpCircleOutline" />
      </a>
    </div>
    <n-switch :value="enabled" :loading="saving" :disabled="saving" @update:value="onChange" />
  </div>
</template>
