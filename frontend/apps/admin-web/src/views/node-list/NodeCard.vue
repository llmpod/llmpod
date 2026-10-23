<script setup lang="ts">
import {
  nodeStatusMap,
  gpuSummary,
  formatPercent,
  formatGbPair,
  progressColor,
  type ComputeNode,
} from './useStore'
import { ChevronDown } from '@vicons/ionicons5'

const props = defineProps<{
  node: ComputeNode
}>()

const emit = defineEmits<{
  gpu: [node: ComputeNode]
  detail: [node: ComputeNode]
}>()

const statusConf = computed(
  () => nodeStatusMap[props.node.status] ?? { label: props.node.status, type: 'default' },
)
const summary = computed(() => gpuSummary(props.node.gpus))

/** 离线节点 runtime 为 null,各字段渲染 —,不把 0 当真实值 */
const runtime = computed(() => props.node.runtime)

/** CPU 宽度百分比 */
const cpuWidth = computed(() => {
  const v = runtime.value?.cpu_percent ?? 0
  return Math.min(100, Math.max(0, Math.round(v)))
})

/** 内存宽度百分比 */
const memoryWidth = computed(() => {
  const v = runtime.value?.memory_percent ?? 0
  return Math.min(100, Math.max(0, Math.round(v)))
})

/** 磁盘宽度百分比 */
const diskWidth = computed(() => {
  const v = runtime.value?.disk_percent ?? 0
  return Math.min(100, Math.max(0, Math.round(v)))
})

/** GPU 负载宽度百分比 */
const gpuWidth = computed(() => {
  const v = runtime.value?.gpu_utilization ?? 0
  return Math.min(100, Math.max(0, Math.round(v)))
})

/** CPU 进度条颜色 */
const cpuColor = computed(() => progressColor(runtime.value?.cpu_percent ?? 0))

/** 内存进度条颜色 */
const memoryColor = computed(() => progressColor(runtime.value?.memory_percent ?? 0))

/** 磁盘进度条颜色 */
const diskColor = computed(() => progressColor(runtime.value?.disk_percent ?? 0))

/** GPU 负载进度条颜色 */
const gpuColor = computed(() => progressColor(runtime.value?.gpu_utilization ?? 0))

/** GPU 汇总文本是否溢出(决定 tooltip 是否启用) */
const gpuLabelRef = ref<HTMLElement>()
const summaryOverflow = ref(false)

function checkSummaryOverflow() {
  const el = gpuLabelRef.value
  if (!el) return
  summaryOverflow.value = el.scrollWidth > el.clientWidth
}

onMounted(() => {
  checkSummaryOverflow()
  window.addEventListener('resize', checkSummaryOverflow)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', checkSummaryOverflow)
})

/** 操作菜单 */
const actionOptions = [
  { label: 'GPU', key: 'gpu' },
  { label: '详情', key: 'detail' },
]

function onActionSelect(key: string) {
  if (key === 'gpu') emit('gpu', props.node)
  if (key === 'detail') emit('detail', props.node)
}
</script>

<template>
  <div class="border border-line rounded-lg p-[1.2rem] flex flex-col gap-[.8rem]">
    <!-- 头部:主机名 + IP + 状态 -->
    <div class="flex items-start justify-between gap-[.8rem]">
      <div class="flex flex-col gap-[.2rem] min-w-0">
        <span class="text-lg font-bold truncate">{{ node.hostname }}</span>
        <span class="text-base text-secondary">{{ node.ip }}</span>
      </div>
      <n-tag size="small" :type="statusConf.type" :bordered="false">
        {{ statusConf.label }}
      </n-tag>
    </div>

    <!-- GPU 汇总(可点击打开 GPU 信息弹窗) -->
    <div class="flex items-center gap-[.4rem] justify-between">
      <span class="text-base text-secondary shrink-0">GPU</span>
      <n-tooltip v-if="node.gpus.length" trigger="hover" :disabled="!summaryOverflow">
        <template #trigger>
          <span
            ref="gpuLabelRef"
            class="text-primary cursor-pointer text-base truncate max-w-[80%]"
            @click="emit('gpu', node)"
          >
            {{ summary.label }}
          </span>
        </template>
        {{ summary.label }}
      </n-tooltip>
      <span v-else class="text-base text-secondary">—</span>
    </div>

    <!-- 资源用量进度条 -->
    <div class="flex flex-col gap-[.6rem]">
      <!-- CPU -->
      <div class="flex flex-col gap-[.4rem]">
        <div class="flex items-center justify-between">
          <span class="text-base text-secondary">CPU</span>
          <span class="text-base" :class="runtime ? '' : 'text-secondary'">{{
            runtime ? formatPercent(runtime.cpu_percent) : '—'
          }}</span>
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 overflow-hidden">
          <div
            v-if="runtime"
            class="h-full rounded-full"
            :class="cpuColor"
            :style="{ width: `${cpuWidth}%` }"
          />
        </div>
      </div>

      <!-- 内存 -->
      <div
        class="flex flex-col gap-[.4rem]"
        :title="
          runtime ? `已用 ${formatGbPair(runtime.memory_used_gb, node.memory_gb)}` : undefined
        "
      >
        <div class="flex items-center justify-between">
          <span class="text-base text-secondary">内存</span>
          <span class="text-base" :class="runtime ? '' : 'text-secondary'">{{
            runtime ? formatPercent(runtime.memory_percent) : '—'
          }}</span>
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 overflow-hidden">
          <div
            v-if="runtime"
            class="h-full rounded-full"
            :class="memoryColor"
            :style="{ width: `${memoryWidth}%` }"
          />
        </div>
      </div>

      <!-- 磁盘 -->
      <div
        class="flex flex-col gap-[.4rem]"
        :title="runtime ? formatGbPair(runtime.disk_used_gb, runtime.disk_total_gb) : undefined"
      >
        <div class="flex items-center justify-between">
          <span class="text-base text-secondary">磁盘</span>
          <span class="text-base" :class="runtime ? '' : 'text-secondary'">{{
            runtime ? formatPercent(runtime.disk_percent) : '—'
          }}</span>
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 overflow-hidden">
          <div
            v-if="runtime"
            class="h-full rounded-full"
            :class="diskColor"
            :style="{ width: `${diskWidth}%` }"
          />
        </div>
      </div>

      <!-- GPU 负载(无 GPU 时禁用) -->
      <div
        class="flex flex-col gap-[.4rem]"
        :title="
          runtime && runtime.gpu_count
            ? `显存 ${formatGbPair(runtime.used_vram_gb, runtime.total_vram_gb)}`
            : undefined
        "
      >
        <div class="flex items-center justify-between">
          <span class="text-base text-secondary">GPU</span>
          <span
            class="text-base"
            :class="runtime && runtime.gpu_count ? '' : 'text-secondary'"
            >{{ runtime && runtime.gpu_count ? formatPercent(runtime.gpu_utilization) : '—' }}</span
          >
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 overflow-hidden">
          <div
            v-if="runtime && runtime.gpu_count"
            class="h-full rounded-full"
            :class="gpuColor"
            :style="{ width: `${gpuWidth}%` }"
          />
        </div>
      </div>
    </div>

    <!-- 底部操作 -->
    <div class="flex justify-end mt-auto pt-[.4rem]">
      <n-dropdown :options="actionOptions" trigger="hover" size="small" @select="onActionSelect">
        <n-button text size="small">
          <div class="flex items-center">
            操作
            <n-icon>
              <ChevronDown />
            </n-icon>
          </div>
        </n-button>
      </n-dropdown>
    </div>
  </div>
</template>
