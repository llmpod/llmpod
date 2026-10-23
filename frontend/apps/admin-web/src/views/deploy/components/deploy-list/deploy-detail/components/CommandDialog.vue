<script setup lang="ts">
import { CheckmarkOutline, CopyOutline, TerminalOutline } from '@vicons/ionicons5'
import { message, copyToClipboard } from '@repo/hooks'
import { getInstanceCommand, type InstanceCommand } from '@/api/deployment'

/**
 * 实例启动命令弹窗体(GET .../instances/{iid}/command):展示该容器下发那一刻的
 * 完整 docker run(后端从 START 任务 payload 快照渲染,不受部署后配置漂移影响)。
 * 命令不可变——打开拉一次,不轮询;关闭重开即取新(重启后为新一轮下发)。
 */
const props = defineProps<{
  deploymentId: string
  instanceId: string
}>()

const loading = ref(true)
const data = ref<InstanceCommand | null>(null)

onMounted(async () => {
  try {
    data.value = await getInstanceCommand(props.deploymentId, props.instanceId)
  } catch {
    // HTTP 层已统一报错,弹窗保持空态
  } finally {
    loading.value = false
  }
})

/** 复制成功后短暂显示对勾 */
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | undefined

async function copyCommand() {
  const command = data.value?.command
  if (!command) return
  try {
    await copyToClipboard(command)
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1500)
  } catch {
    message.error('复制失败,请手动复制')
  }
}

/** 次区字段行:命令以外的下发元信息 */
const metaRows = computed(() => {
  const item = data.value
  if (!item) return []
  return [
    ['镜像', item.image],
    ['容器名', item.container_name],
    ['下发任务', item.task_status ? `${item.task_status} · 第 ${item.attempt} 次` : null],
    ['下发时间', item.dispatched_at ? new Date(item.dispatched_at).toLocaleString() : null],
  ] as const
})

/** 命令是否疑似含敏感环境变量(TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL/AK 结尾):命中时警示勿外传 */
const hasSensitiveEnv = computed(() => {
  const command = data.value?.command
  if (!command) return false
  return /(^|\s)-e\s+\w*(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)\w*=|(^|\s)--env\s+\w*(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)\w*=|\w+(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)\w*=\S+/.test(
    command,
  )
})
</script>

<template>
  <div class="flex flex-col">
    <n-spin v-if="loading" class="py-[6rem]" />

    <template v-else-if="data?.found">
      <!-- 敏感信息警示:命令含 TOKEN/KEY/SECRET 等环境变量时提示(完整命令原样展示属排障用途,复制/截图勿外传) -->
      <LmAlert
        v-if="hasSensitiveEnv"
        type="warning"
        class="mb-[.8rem]"
      >
        命令疑似包含敏感环境变量(令牌/密钥),请勿截图或粘贴到工单/群聊等外部渠道
      </LmAlert>
      <!-- 主区:终端风格深色块渲染完整命令(固定深底,明暗主题一致) + 复制 -->
      <div class="relative">
        <n-button
          size="tiny"
          quaternary
          class="absolute top-[.5rem] right-[.5rem] z-1 !text-gray-300 hover:!text-white"
          @click="copyCommand"
        >
          <template #icon>
            <n-icon :component="copied ? CheckmarkOutline : CopyOutline" />
          </template>
          {{ copied ? '已复制' : '复制' }}
        </n-button>
        <div
          class="overflow-auto rounded-md border border-line bg-gray-900 px-[1rem] py-[.8rem] font-mono text-xs leading-[1.8] text-gray-100 whitespace-pre-wrap break-all"
        >{{ data.command }}</div>
      </div>

      <!-- 次区:下发元信息(命令来源/时间) -->
      <div class="mt-[1rem] rounded-md border border-line px-[1rem] py-[.6rem] text-small">
        <div v-for="[label, value] in metaRows" :key="label" class="flex gap-[.8rem] py-[.2rem]">
          <span class="text-secondary shrink-0">{{ label }}</span>
          <span class="break-all">{{ value ?? '—' }}</span>
        </div>
      </div>
    </template>

    <div v-else class="py-[6rem]">
      <n-empty size="small" description="暂无启动命令(重启窗口期或未下发过)">
        <template #icon>
          <n-icon :component="TerminalOutline" />
        </template>
      </n-empty>
    </div>
  </div>
</template>
