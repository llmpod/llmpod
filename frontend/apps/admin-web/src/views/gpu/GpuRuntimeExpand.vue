<script setup lang="ts">
import { formatGbPair, formatPercent, progressColor, type GpuRuntime } from './useStore'

/**
 * 表格行展开内容:GPU 实时指标摘要(runtime 来自节点心跳镜像)。
 * 只展示运维最关注的四项(核心利用率/显存利用率/温度/功耗),风扇转速、
 * 时钟频率、进程数等低频指标不铺开;利用率配迷你进度条并按阈值着色。
 * runtime 为 null(节点离线或心跳未上报)时展示空态文案,不把缺数据当 0 渲染。
 */
const props = defineProps<{ runtime: GpuRuntime | null; totalVramGb: number }>()

/** 迷你进度条宽度(%),clamp 到 0-100 */
function barWidth(percent: number): number {
  return Math.min(100, Math.max(0, percent))
}
</script>

<template>
  <div
    v-if="props.runtime"
    class="rounded-md border border-line-strong bg-fill-2 px-[1.2rem] py-[.8rem] text-small"
  >
    <div class="grid grid-cols-2 gap-x-[2rem] gap-y-[.8rem] lg:grid-cols-4">
      <!-- GPU 核心利用率 -->
      <div class="flex flex-col gap-[.3rem]">
        <span class="text-xs text-secondary">GPU 利用率</span>
        <div class="flex items-center gap-[.8rem]">
          <span class="tabular-nums font-medium">{{ formatPercent(props.runtime.utilization) }}</span>
          <div class="h-[.4rem] w-[6rem] overflow-hidden rounded-full bg-fill-4">
            <div
              class="h-full rounded-full"
              :class="progressColor(props.runtime.utilization)"
              :style="{ width: `${barWidth(props.runtime.utilization)}%` }"
            />
          </div>
        </div>
      </div>
      <!-- 显存利用率:副文本交代已用/总量 -->
      <div class="flex flex-col gap-[.3rem]">
        <span class="text-xs text-secondary">显存利用率</span>
        <div class="flex items-center gap-[.8rem]">
          <span class="tabular-nums font-medium">{{
            formatPercent(props.runtime.memory_utilization)
          }}</span>
          <div class="h-[.4rem] w-[6rem] overflow-hidden rounded-full bg-fill-4">
            <div
              class="h-full rounded-full"
              :class="progressColor(props.runtime.memory_utilization)"
              :style="{ width: `${barWidth(props.runtime.memory_utilization)}%` }"
            />
          </div>
        </div>
        <span class="text-xs text-secondary">
          已用 {{ formatGbPair(props.runtime.used_vram_gb, props.totalVramGb) }}
        </span>
      </div>
      <!-- 温度 -->
      <div class="flex flex-col gap-[.3rem]">
        <span class="text-xs text-secondary">温度</span>
        <span class="tabular-nums font-medium">{{ props.runtime.temperature }}℃</span>
      </div>
      <!-- 功耗:副文本交代功耗上限 -->
      <div class="flex flex-col gap-[.3rem]">
        <span class="text-xs text-secondary">功耗</span>
        <span class="tabular-nums font-medium">{{ props.runtime.power_draw_w }}W</span>
        <span class="text-xs text-secondary">上限 {{ props.runtime.power_limit_w }}W</span>
      </div>
    </div>
  </div>
  <div v-else class="px-[.4rem] py-[.6rem] text-small text-secondary">
    暂无实时数据（节点离线或心跳未上报）
  </div>
</template>
