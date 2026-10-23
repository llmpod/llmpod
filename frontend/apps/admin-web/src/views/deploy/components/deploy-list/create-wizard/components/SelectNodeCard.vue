<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { CheckmarkCircle, DesktopOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import { NODE_STATUS_MAP } from '@/views/node-pool/topology/useStore'
import type { TopologyNode } from '@/api/topology'
import { REPLICA_COLORS, useWizardStore } from '../useStore'

const props = defineProps<NodeProps>()
const store = useWizardStore()

const nodeItem = computed(() => props.data.record as TopologyNode)
const statusDot = computed(() => NODE_STATUS_MAP[nodeItem.value.status]?.dot ?? 'bg-secondary')
const statusLabel = computed(
  () => NODE_STATUS_MAP[nodeItem.value.status]?.label ?? nodeItem.value.status,
)

/** 该节点下可加入激活副本的 GPU 与已加入数;已属其它副本的卡不参与激活份勾选 */
const selectable = computed(() => store.selectableGpusOf(nodeItem.value.id))
const selectedCount = computed(
  () => selectable.value.filter((gpu) => store.selectedGpuUuids.value.has(gpu.uuid)).length,
)
/** 该节点下已被其它副本占用的卡数(摘要补充口径:如「另 2 张已属其它副本」) */
const ownedElsewhereCount = computed(
  () =>
    (store.topology.value?.gpus ?? []).filter(
      (gpu) =>
        gpu.node_id === nodeItem.value.id &&
        !store.selectedGpuUuids.value.has(gpu.uuid) &&
        store.replicaIndexOfUuid.value.has(gpu.uuid),
    ).length,
)
/** 全选/半选态:驱动卡片选中样式 */
const allSelected = computed(
  () => selectable.value.length > 0 && selectedCount.value === selectable.value.length,
)
const partialSelected = computed(() => selectedCount.value > 0 && !allSelected.value)

/** 钉定到本节点的份列表(CPU 逐份钉定;含激活份与否区分展示) */
const pinnedReplicas = computed(() => {
  const activeIndex = store.activeReplicaIndex.value
  return store.cpuNodeSelections.value
    .map((nodeId, index) => ({ nodeId, index }))
    .filter((item): item is { nodeId: string; index: number } => item.nodeId === nodeItem.value.id)
    .map((item) => ({
      seq: item.index + 1,
      color: REPLICA_COLORS[item.index % REPLICA_COLORS.length],
      active: item.index === activeIndex,
    }))
})
/** 激活副本已钉本节点(卡片选中态;再点=取消本份钉定) */
const cpuSelected = computed(() => store.activeReplicaCpuNodeId.value === nodeItem.value.id)
/** 本节点承载的钉定份数(同节点多份的容量参考,徽章行标题) */
const pinnedCount = computed(() => pinnedReplicas.value.length)
const cpuAvailable = computed(() => store.isCpuNodeSelectable(nodeItem.value))
/**
 * 卡片色环:多副本且有钉定时按份取色(与 GPU 卡色环同口径,边框优先于 selected 主色)。
 * 激活份钉在本节点 → 激活份颜色;否则取首份颜色(多份钉定时徽章行承担逐份区分)。
 * 单副本不带色环(与 GPU 一致,走 selected 主色)。
 */
const pairColor = computed(() => {
  if (store.replicaSelections.value.length <= 1 || !pinnedReplicas.value.length) return undefined
  const activePinned = pinnedReplicas.value.find((replica) => replica.active)
  return (activePinned ?? pinnedReplicas.value[0]).color
})
/** 卡片选中态:CPU 落点按激活份钉定;GPU 节点按卡全选/半选 */
const selected = computed(() =>
  cpuSelected.value ? true : allSelected.value || partialSelected.value,
)
/** 置灰态:CPU 落点节点不灰(点卡片即走 CPU 部署);其余无可选卡时灰 */
const dimmed = computed(() =>
  cpuSelected.value || cpuAvailable.value ? false : !selectable.value.length,
)

/** 实时负载:CPU 使用率与内存可用/总量(选节点参考;离线为 0 → 展示 --) */
const cpuLabel = computed(() =>
  nodeItem.value.status === 'online' ? `${Math.round(nodeItem.value.cpu_percent ?? 0)}%` : '--',
)
const memoryLabel = computed(() => {
  const total = nodeItem.value.memory_gb ?? 0
  if (!total) return '--'
  return `${(nodeItem.value.available_memory_gb ?? 0).toFixed(1)}/${total.toFixed(1)}GB`
})

/** 纯 CPU 档的每份内存需求(逐层表 0 档 ram_gb;cpu_only 口径已含落内存 KV,不加 kv_gb) */
const cpuRamNeed = computed(() => {
  const memory = store.inspect.value?.memory
  if (!memory?.tiers.length) return null
  return memory.tiers[0]?.ram_gb ?? 0
})
/** 该节点内存能否装下本节点的全部钉定份数(需求或账本可用内存未知 → null,不误报) */
const cpuFits = computed(() => {
  const need = cpuRamNeed.value
  if (need === null) return null
  const support = store.nodeSupportMap.value.get(nodeItem.value.id)
  if (!support) return null
  return (support.available_memory_gb ?? 0) + 1e-6 >= need * Math.max(1, pinnedCount.value)
})

/** 不可选原因:节点离线优先,其次 inspect node_support(引擎+镜像视角),最后回退无可选 GPU */
const disabledReason = computed(() => {
  if (nodeItem.value.status !== 'online')
    return NODE_STATUS_MAP[nodeItem.value.status]?.label ?? nodeItem.value.status
  return store.nodeDisabledReason(nodeItem.value) ?? '无可选 GPU'
})
</script>

<template>
  <NodeCard
    :icon="DesktopOutline"
    :title="nodeItem.hostname"
    :subtitle="nodeItem.ip"
    :status-color="statusDot"
    :selected="selected"
    :dimmed="dimmed"
    :ring-color="pairColor"
  >
    <div class="flex justify-between">
      <span class="text-secondary">状态</span><span>{{ statusLabel }}</span>
    </div>
    <!-- 实时负载:CPU 使用率 + 内存可用/总量(选节点参考) -->
    <div class="flex justify-between gap-[.6rem]">
      <span class="text-secondary shrink-0">CPU</span>
      <span class="text-right">{{ cpuLabel }}</span>
    </div>
    <div class="flex justify-between gap-[.6rem]">
      <span class="text-secondary shrink-0">内存</span>
      <span class="text-right">{{ memoryLabel }}</span>
    </div>
    <!-- CPU 钉定份徽章行:每份一枚彩色徽章(描边/文字/色点均为该份颜色,与 GPU 色环同盘),
         激活份另加软底强调;同节点多份显示多枚 -->
    <div v-if="pinnedReplicas.length" class="flex items-center gap-[.4rem] flex-wrap">
      <span class="text-secondary shrink-0">钉定副本</span>
      <span
        v-for="replica in pinnedReplicas"
        :key="replica.seq"
        class="inline-flex items-center gap-[.3rem] rounded-[.4rem] px-[.5rem] py-[.1rem] text-xs border"
        :class="replica.active ? 'font-medium' : ''"
        :style="{
          color: replica.color,
          borderColor: replica.color,
          backgroundColor: replica.active ? `${replica.color}33` : undefined,
        }"
      >
        <span
          class="w-[.8rem] h-[.8rem] rounded-full shrink-0"
          :style="{ backgroundColor: replica.color }"
        />
        {{ replica.seq }}
      </span>
    </div>
    <!-- CPU 落点节点(激活份已钉):无卡部署,部署方式带份颜色与勾标(与 GPU 归属角标同构) -->
    <template v-if="cpuSelected">
      <div class="flex justify-between gap-[.6rem]">
        <span class="text-secondary shrink-0">部署方式</span>
        <span
          class="text-right inline-flex items-center gap-[.3rem]"
          :class="pairColor ? '' : 'text-primary'"
          :style="pairColor ? { color: pairColor } : undefined"
        >
          <n-icon :size="14"><CheckmarkCircle /></n-icon>
          CPU 部署 · 本副本已选
        </span>
      </div>
    </template>
    <template v-else-if="pinnedCount">
      <!-- 本节点被其它副本钉定(激活份未钉):部署方式按份着色展示归属 -->
      <div class="flex justify-between gap-[.6rem]">
        <span class="text-secondary shrink-0">部署方式</span>
        <span class="text-right inline-flex items-center gap-[.4rem] flex-wrap justify-end">
          <span
            v-for="replica in pinnedReplicas"
            :key="replica.seq"
            class="inline-flex items-center gap-[.3rem] rounded-[.4rem] px-[.5rem] py-[.1rem] text-xs border"
            :style="{ color: replica.color, borderColor: replica.color }"
          >
            <span
              class="w-[.8rem] h-[.8rem] rounded-full shrink-0"
              :style="{ backgroundColor: replica.color }"
            />
            副本{{ replica.seq }}
          </span>
          <span>CPU 部署</span>
        </span>
      </div>
    </template>
    <template v-else>
      <div
        v-if="selectable.length"
        class="flex justify-between gap-[.6rem]"
      >
        <span class="text-secondary shrink-0">可选 GPU</span>
        <span class="text-right">
          已选
          <span :class="selectedCount ? 'text-primary font-semibold' : ''">
            {{ selectedCount }}
          </span>
          /{{ selectable.length }} 张
        </span>
      </div>
      <!-- 无可选 GPU:红色截断展示原因,悬浮行上 tooltip 展示完整原因 -->
      <n-tooltip v-else trigger="hover">
        <template #trigger>
          <div class="flex justify-between gap-[.6rem]">
            <span class="text-secondary shrink-0">可选 GPU</span>
            <span class="text-error truncate text-right min-w-0">{{ disabledReason }}</span>
          </div>
        </template>
        <!-- 限宽换行:长原因不溢出屏幕 -->
        <div class="max-w-[24rem] break-words">{{ disabledReason }}</div>
      </n-tooltip>
      <!-- CPU 部署入口:点卡片即为激活副本钉定纯 CPU(不选卡);有可用卡的节点同样支持。
           内存装不下(按已钉份数累加)时标红提示(仍可点,后端预检会给出精确原因)。 -->
      <div v-if="cpuAvailable" class="flex justify-between gap-[.6rem]">
        <span class="text-secondary shrink-0">部署方式</span>
        <n-tooltip v-if="cpuFits === false" trigger="hover">
          <template #trigger>
            <span class="text-right text-error">CPU 部署 · 内存不足</span>
          </template>
          纯 CPU 每份需约 {{ cpuRamNeed?.toFixed(1) }}GB ×
          {{ Math.max(1, pinnedCount) }} 份,该节点可用内存不足
        </n-tooltip>
        <span v-else class="text-right text-primary">CPU 部署</span>
      </div>
    </template>
  </NodeCard>
</template>
