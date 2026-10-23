<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { HardwareChipOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import { gpuBrandIcon } from '@/views/node-pool/topology/components/brandIcons'
import { GPU_STATUS_MAP } from '@/views/node-pool/topology/useStore'
import type { GpuNodeData } from '../graph'

const props = defineProps<NodeProps>()
const gpu = computed(() => props.data.record as GpuNodeData)
/** GPU 厂商品牌图标(NVIDIA/昇腾),未命中回退通用芯片图标 */
const icon = computed(() => gpuBrandIcon(gpu.value.name) ?? HardwareChipOutline)
const statusDot = computed(() => GPU_STATUS_MAP[gpu.value.status]?.dot ?? 'bg-secondary')
/** 显存占用百分比(已用/总量);总量为 0 时归 0 防除零 */
const vramPercent = computed(() =>
  gpu.value.total_vram_gb > 0
    ? Math.min(100, Math.round((gpu.value.used_vram_gb / gpu.value.total_vram_gb) * 100))
    : 0,
)
</script>

<template>
  <!-- 部署占用 GPU 卡片:实时显存/利用率(数据源集群拓扑,随轮询刷新);
       详情面板展示 UUID/所属节点等低频信息 -->
  <NodeCard
    :icon="icon"
    :title="gpu.name"
    :subtitle="`#${gpu.index}`"
    :status-color="statusDot"
  >
    <div>
      <div class="flex justify-between">
        <span class="text-secondary">显存</span>
        <span>{{ gpu.used_vram_gb }}/{{ gpu.total_vram_gb }}GB</span>
      </div>
      <div class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
        <div
          class="h-full rounded-full bg-primary"
          :style="{ width: `${vramPercent}%` }"
        />
      </div>
    </div>
    <div>
      <div class="flex justify-between">
        <span class="text-secondary">利用率</span>
        <span>{{ gpu.util != null ? `${gpu.util}%` : '--' }}</span>
      </div>
      <div class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
        <div
          class="h-full rounded-full bg-primary"
          :style="{ width: `${gpu.util ?? 0}%` }"
        />
      </div>
    </div>
  </NodeCard>
</template>
