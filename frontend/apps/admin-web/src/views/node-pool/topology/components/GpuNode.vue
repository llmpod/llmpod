<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { HardwareChipOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import { gpuBrandIcon } from './brandIcons'
import { GPU_STATUS_MAP, type GpuNodeData } from '../useStore'

const props = defineProps<NodeProps>()
const gpu = computed(() => props.data.record as GpuNodeData)
const detail = computed(() => Boolean(props.data.detail))
const statusDot = computed(() => GPU_STATUS_MAP[gpu.value.status]?.dot ?? 'bg-secondary')
/** GPU 厂商品牌图标(NVIDIA/昇腾),未命中回退通用芯片图标 */
const icon = computed(() => gpuBrandIcon(gpu.value.name) ?? HardwareChipOutline)
/** 显存占用百分比(已用/总量);总量为 0 时归 0 防除零 */
const vramPercent = computed(() =>
  gpu.value.memory > 0 ? Math.min(100, Math.round((gpu.value.usedVramGb / gpu.value.memory) * 100)) : 0,
)
</script>

<template>
  <NodeCard
    :icon="icon"
    :title="gpu.name"
    :subtitle="`#${gpu.id}`"
    :status-color="statusDot"
  >
    <template v-if="detail">
      <div>
        <div class="flex justify-between">
          <span class="text-secondary">显存</span><span>{{ gpu.usedVramGb }}/{{ gpu.memory }}GB</span>
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
          <div class="h-full rounded-full bg-primary" :style="{ width: `${vramPercent}%` }" />
        </div>
      </div>
      <div>
        <div class="flex justify-between">
          <span class="text-secondary">利用率</span><span>{{ gpu.util }}%</span>
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
          <div class="h-full rounded-full bg-primary" :style="{ width: `${gpu.util}%` }" />
        </div>
      </div>
    </template>
  </NodeCard>
</template>
