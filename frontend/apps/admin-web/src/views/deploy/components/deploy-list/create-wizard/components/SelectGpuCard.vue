<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { CheckmarkCircle, HardwareChipOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import { gpuBrandIcon } from '@/views/node-pool/topology/components/brandIcons'
import { GPU_STATUS_MAP } from '@/views/node-pool/topology/useStore'
import type { TopologyGpu } from '@/api/topology'
import { REPLICA_COLORS, useWizardStore } from '../useStore'

const props = defineProps<NodeProps>()
const store = useWizardStore()

const gpu = computed(() => props.data.record as TopologyGpu)
const statusDot = computed(() => GPU_STATUS_MAP[gpu.value.status]?.dot ?? 'bg-secondary')
/** GPU 厂商品牌图标(NVIDIA/昇腾),未命中回退通用芯片图标 */
const icon = computed(() => gpuBrandIcon(gpu.value.name) ?? HardwareChipOutline)

/** 选中态:属于当前编辑副本(画布勾选只作用于激活副本) */
const selected = computed(() => store.selectedGpuUuids.value.has(gpu.value.uuid))

/** 所属副本序号(1 起;未选为 null):色环/角标定位,多副本才显示份标识 */
const ownerSeq = computed(() => store.replicaIndexOfUuid.value.get(gpu.value.uuid) ?? null)

/** 已属其它副本(未被激活份选中但已有归属):正常亮度展示 + 色环/角标标识归属,不走禁选视觉 */
const ownedByOther = computed(() => ownerSeq.value !== null && !selected.value)

/** 可加入激活副本(在线/未占用/未灰化/未被任何份选中/激活份桶约束) */
const selectable = computed(() => store.isGpuSelectable(gpu.value))

/** 色环边框色:按所属副本序号取色(与副本卡片色点同盘);单副本/未选不带色环 */
const pairColor = computed(() =>
  store.replicaSelections.value.length > 1 && ownerSeq.value !== null
    ? REPLICA_COLORS[(ownerSeq.value - 1) % REPLICA_COLORS.length]
    : undefined,
)
/** 份内非单 (节点,架构) 桶时虚线(防御态提示,正常选择路径不可达) */
const pairDashed = computed(() => {
  const pairs = store.pairingPreview.value.pairs
  if (ownerSeq.value === null) return false
  return pairs.some((pair) => pair.seq === ownerSeq.value && !pair.complete)
})
/** 选中角标文案:激活份已选带份序号(如「已选 · 副本2」),非激活份显示归属(如「副本1」) */
const selectedLabel = computed(() => {
  if (ownerSeq.value === null) return '已选择'
  return selected.value ? `已选 · 副本${ownerSeq.value}` : `副本${ownerSeq.value}`
})
/** inspect 逐卡评估(灰化原因/承载力/显存需求);预检未加载时为 undefined */
const assessed = computed(() => store.inspectGpuMap.value.get(gpu.value.uuid))
/** 是否被运行中/创建中部署占用(拓扑 status-aware 口径);不再禁选,转橙色告警行引导手选共用显存 */
const occupied = computed(() => store.occupiedGpuUuids.value.has(gpu.value.uuid))
/** 占用告警归因(声明总量/剩余可分配/部署名;inspect 未加载或归因缺失时为 null,回退通用短句) */
const occupancy = computed(() => store.gpuOccupancyMap.value.get(gpu.value.uuid) ?? null)
/** 占用告警行文案:亮出被哪个部署占多少 GB、剩余多少 GB 可选用;多部署名「、」连接 */
const occupancyWarning = computed(() => {
  if (!occupancy.value) return '已被部署占用,可手选共用显存'
  const names = occupancy.value.names.join('、')
  return `已被 ${names} 占用 ${occupancy.value.declaredGb.toFixed(1)}GB,剩余 ${occupancy.value.allocatableGb.toFixed(1)}GB 可选用`
})
/** 占用告警悬浮:完整归因与共用显存出路提示 */
const occupancyTooltip = computed(() => {
  if (!occupancy.value) return '已被部署占用,可手选共用显存'
  const names = occupancy.value.names.join('、')
  return `当前 GPU 已被部署 ${names} 占用 ${occupancy.value.declaredGb.toFixed(1)}GB 显存,剩余约 ${occupancy.value.allocatableGb.toFixed(1)}GB 可选用;共用显存请在确认部署时调低显存占比`
})
/** 禁选原因:inspect 评估(架构不匹配/离线/其它份占用/不同组)优先,其次离线 */
const disabledReason = computed(() => {
  const reason = store.inspectDisabledReason(gpu.value)
  if (reason) return reason
  if (gpu.value.status !== 'online') return GPU_STATUS_MAP[gpu.value.status]?.label ?? '不可用'
  return '节点不在线'
})
</script>

<template>
  <NodeCard
    :icon="icon"
    :title="gpu.name"
    :subtitle="`GPU ${gpu.index}`"
    :status-color="statusDot"
    :selected="selected"
    :dimmed="!selectable && !ownedByOther"
    :ring-color="pairColor"
    :dashed="pairDashed"
  >
    <!-- 不可选原因:红色截断,悬浮行上 tooltip 展示完整原因;已属其它副本的卡走正常信息展示 -->
    <n-tooltip v-if="!selectable && !ownedByOther" trigger="hover">
      <template #trigger>
        <div class="flex justify-between gap-[.6rem]">
          <span class="text-secondary shrink-0">不可选</span>
          <span class="text-error truncate text-right min-w-0">{{ disabledReason }}</span>
        </div>
      </template>
      <!-- 限宽换行:长原因(镜像/驱动详情)不溢出屏幕 -->
      <div class="max-w-[24rem] break-words">{{ disabledReason }}</div>
    </n-tooltip>
    <template v-else>
      <div class="flex justify-between">
        <span class="text-secondary">显存</span>
        <span>
          <template v-if="gpu.available_vram_gb !== null">
            可用 {{ gpu.available_vram_gb }}/{{ gpu.total_vram_gb }}GB
          </template>
          <template v-else>{{ gpu.total_vram_gb }}GB</template>
        </span>
      </div>
      <div v-if="gpu.util !== null">
        <div class="flex justify-between">
          <span class="text-secondary">利用率</span><span>{{ gpu.util }}%</span>
        </div>
        <div class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
          <div class="h-full rounded-full bg-primary" :style="{ width: `${gpu.util}%` }" />
        </div>
      </div>
      <!-- 预检评估:单卡承载力 + 模型整体显存需求 -->
      <div v-if="assessed" class="flex justify-between">
        <span class="text-secondary whitespace-nowrap">评估</span>
        <span class="text-right">
          {{ assessed.fit ? '单卡可承载' : '需多卡 TP' }} · 需求
          {{ assessed.required_vram_gb !== null ? `${assessed.required_vram_gb}GB` : '--' }}
        </span>
      </div>
      <!-- 占用告警:被运行中部署占用的卡正常可选(共用显存逐张手选),橙色亮出归因与剩余 -->
      <n-tooltip v-if="occupied" trigger="hover">
        <template #trigger>
          <div class="flex justify-between gap-[.6rem]">
            <span class="text-secondary shrink-0">占用</span>
            <span class="text-warning truncate text-right min-w-0">{{ occupancyWarning }}</span>
          </div>
        </template>
        <!-- 限宽换行:长归因(多部署名)不溢出屏幕 -->
        <div class="max-w-[24rem] break-words">{{ occupancyTooltip }}</div>
      </n-tooltip>
    </template>
    <!-- 归属角标:激活份已选/非激活份归属,多副本带份序号(与副本卡片色点同色) -->
    <div
      v-if="selected || ownerSeq !== null"
      class="flex items-center gap-[.3rem]"
      :style="pairColor ? { color: pairColor } : undefined"
    >
      <n-icon :size="14">
        <CheckmarkCircle />
      </n-icon>
      <span>{{ selectedLabel }}</span>
    </div>
  </NodeCard>
</template>
