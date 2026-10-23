<script setup lang="ts">
import {
  SelectionMode,
  useKeyPress,
  useNodesInitialized,
  useVueFlow,
  VueFlow,
} from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import {
  AddOutline,
  CheckboxOutline,
  CloseOutline,
  ChevronDownOutline,
  InvertModeOutline,
  ScanOutline,
  SettingsOutline,
} from '@vicons/ionicons5'
import { message, useConfirm } from '@repo/hooks'
import type { TopologyGpu, TopologyNode } from '@/api/topology'
import SelectClusterCard from '../components/SelectClusterCard.vue'
import SelectGpuCard from '../components/SelectGpuCard.vue'
import SelectNodeCard from '../components/SelectNodeCard.vue'
import { buildSelectGraph } from '../graph'
import { REPLICA_COLORS, REPLICA_COUNT_MAX, useWizardStore } from '../useStore'

const store = useWizardStore()
// 模板里使用的成员必须解构为顶层 ref(store 是普通对象,嵌套 ref 在模板不会自动解包,v-if 恒真)
const {
  topologyLoading,
  topologyError,
  inspectError,
  topologyRuleError,
  selectAll,
  invertSelection,
  clearSelection,
  loadTopology,
  loadInspect,
  kvCacheOptions,
  kvCacheOption,
  kvCacheSelectOptions,
  hasKvCache,
  isLlamaCpp,
} = store

/**
 * 「全选/反选」入口是否展示:功能完整保留,仅默认隐藏 UI 入口;置 true 即恢复展示
 */
const showSelectActions = false

const { onNodeClick, onSelectionEnd, fitView, getSelectedNodes, removeSelectedNodes } = useVueFlow()
/** 全部节点完成尺寸测量后为 true;拓扑异步就绪/重试成功后再适配视图 */
const nodesInitialized = useNodesInitialized()

/**
 * 框选模式:开启后左键拖拽画出选区批量加入 GPU。
 * 视图平移让位给框选,改用空格/鼠标中键拖拽(pan-activation-key-code 默认 Space)、滚轮缩放与 Controls 按钮
 */
const batchMode = ref(false)

/**
 * 空格按住状态;框选模式下动态放行左键平移。
 * Vue Flow 的 d3-zoom 过滤器在 panOnDrag 数组不含 0 时会直接丢弃左键拖拽,
 * panActivationKeyCode(默认 Space)无法绕过该过滤,所以空格按下时把左键(0)加回 panOnDrag
 */
const spacePressed = useKeyPress('Space')
const panOnDrag = computed<boolean | number[]>(() => {
  if (!batchMode.value) return true
  return spacePressed.value ? [0, 1] : [1]
})

/** 拓扑 → 选择图;选中态由卡片组件实时读 store,不在此重建 */
const graph = computed(() =>
  store.topology.value ? buildSelectGraph(store.topology.value) : { nodes: [], edges: [] },
)

watch(nodesInitialized, (ready) => {
  if (ready && graph.value.nodes.length) fitView({ padding: 0.2, duration: 300 })
})

/**
 * 节点卡点击:引擎声明 cpu(llama.cpp)= 为激活副本钉定/取消该节点纯 CPU 部署(不选卡);
 * 其余引擎 = 整选/取消其下可选 GPU(作用于激活副本);GPU 卡点击 = 勾选/取消激活副本;
 * 已属其它副本的卡不可选(store 禁选并给出原因)。集群卡无操作
 */
onNodeClick(({ node }) => {
  if (node.type === 'gpu') {
    onToggleGpuCard(node.data.record)
    return
  }
  if (node.type !== 'node') return
  const nodeItem = node.data.record as TopologyNode
  if (store.activeReplicaCpuNodeId.value === nodeItem.id || store.isCpuNodeSelectable(nodeItem)) {
    onToggleCpuNodeCard(nodeItem)
    return
  }
  store.toggleNode(nodeItem)
})

/** 点 GPU 卡:CPU 形态下先确认再切换(钉定会被清掉,对称反向切 CPU 亦然) */
async function onToggleGpuCard(gpu: TopologyGpu) {
  if (store.cpuMode.value) {
    const ok = await confirmFormSwitch('显存部署', `将清除 ${store.cpuNodeSelections.value.filter((id) => id).length} 份副本的 CPU 落点钉定`)
    if (!ok) return
  }
  store.toggleGpu(gpu)
}

/** 点节点卡(CPU 入口):显存形态已有选卡时先确认(切换会清掉全部已选卡) */
async function onToggleCpuNodeCard(nodeItem: TopologyNode) {
  if (!store.cpuMode.value && selectedTotal.value) {
    const ok = await confirmFormSwitch('纯 CPU 部署', `将清除已选的 ${selectedTotal.value} 张 GPU`)
    if (!ok) return
  }
  store.toggleCpuNode(nodeItem)
}

/** 形态切换二次确认(GPU↔CPU 互斥,切换会清掉另一形态的全部选择) */
async function confirmFormSwitch(title: string, detail: string): Promise<boolean> {
  try {
    await useConfirm({
      title,
      content: `${detail},是否继续?`,
      confirmText: '切换',
    })
    return true
  } catch {
    return false
  }
}

/** 框选结束:选区内 GPU 并入激活副本,随后清掉 Vue Flow 自身选中态(避免残留高亮与下次混入) */
onSelectionEnd(() => {
  const selected = getSelectedNodes.value
  if (!selected.length) return
  store.addGpus(
    selected.filter((node) => node.type === 'gpu').map((node) => node.data.record as TopologyGpu),
  )
  removeSelectedNodes(selected)
})

/** 关闭框选时清掉 Vue Flow 残留选中态,恢复正常左键平移 */
function toggleBatchMode() {
  batchMode.value = !batchMode.value
  if (!batchMode.value && getSelectedNodes.value.length) {
    removeSelectedNodes(getSelectedNodes.value)
  }
}

// ---------- 副本卡片栏(增删/切换,画布选卡始终作用于激活副本) ----------

/** 副本卡片视图:色点(与画布色环同盘)、张数、节点/架构摘要、激活态 */
const replicaCards = computed(() => {
  const topo = store.topology.value
  const gpuMap = new Map((topo?.gpus ?? []).map((gpu) => [gpu.uuid, gpu] as const))
  const hostnames = new Map((topo?.nodes ?? []).map((node) => [node.id, node.hostname] as const))
  return store.replicaSelections.value.map((uuids, index) => {
    const gpus = uuids.map((uuid) => gpuMap.get(uuid)).filter((gpu): gpu is TopologyGpu => !!gpu)
    const nodeIds = [...new Set(gpus.map((gpu) => gpu.node_id))]
    const archs = [...new Set(gpus.map((gpu) => gpu.arch))]
    const singleBucket = nodeIds.length <= 1 && archs.length <= 1
    let summary: string
    if (store.cpuMode.value) {
      // 逐份节点落点:本份钉的节点名;未钉显示「未选节点」(由 topologyRuleError 指引补齐)
      const pinnedId = store.cpuNodeSelections.value[index]
      summary = pinnedId
        ? `CPU 部署 · ${hostnames.get(pinnedId) ?? pinnedId}`
        : 'CPU 部署 · 未选节点'
    }
    else if (!uuids.length) summary = '未选卡'
    else if (!singleBucket) summary = `${uuids.length} 张 · 跨节点/架构`
    else
      summary = `${hostnames.get(nodeIds[0] ?? '') ?? nodeIds[0] ?? '--'}·${archs[0] ?? ''} ×${uuids.length}`
    return {
      index,
      seq: index + 1,
      color: REPLICA_COLORS[index % REPLICA_COLORS.length],
      summary,
      active: index === store.activeReplicaIndex.value,
    }
  })
})

/** 激活副本序号(人读 1 起) */
const activeReplicaSeq = computed(() => store.activeReplicaIndex.value + 1)

/** 添加副本(上限 REPLICA_COUNT_MAX;新份自动激活,立即在画布为其选卡) */
function onAddReplica() {
  if (!store.addReplica()) message.warning(`最多 ${REPLICA_COUNT_MAX} 份副本`)
}

/** 移除副本:已选卡的份先二次确认(选卡随之取消) */
async function onRemoveReplica(index: number) {
  const count = store.replicaSelections.value[index]?.length ?? 0
  if (count) {
    try {
      await useConfirm({
        title: `移除副本 ${index + 1}`,
        content: `该副本已选 ${count} 张 GPU,移除后将一并取消选择。确认移除?`,
        confirmText: '移除',
      })
    } catch {
      return
    }
  }
  store.removeReplica(index)
}

// ---------- 已选摘要与门控(全量口径 = 所有副本并集) ----------

/** 已选 GPU 总量(全部副本并集) */
const selectedTotal = computed(() => store.allSelectedUuids.value.size)

/** 各副本等量时的每副本卡数(不一致时为 0,由 topologyRuleError 提示) */
const perCopyGpus = computed(() => {
  const lengths = store.replicaSelections.value.map((uuids) => uuids.length)
  if (!lengths.length || lengths.some((length) => length !== lengths[0])) return 0
  return lengths[0]
})

/** 可选 GPU 总数(手选口径:在线 + 未灰化,被占卡计入);驱动 noneSelectable 全集群不可选判定 */
const selectableCount = computed(
  () => (store.topology.value?.gpus ?? []).filter((gpu) => store.isGpuSelectable(gpu)).length,
)

/** 批量口径可选数(全选/反选候选,被占卡不计;共用显存只能逐张手选);驱动批量按钮禁用 */
const batchSelectableCount = computed(
  () => (store.topology.value?.gpus ?? []).filter((gpu) => store.isGpuBatchSelectable(gpu)).length,
)

/** 已选摘要:按节点分组(节点 hostname × 已选卡数,全副本并集口径) */
const selectedGroups = computed(() => {
  const topo = store.topology.value
  if (!topo || !selectedTotal.value) return []
  return topo.nodes
    .map((nodeItem) => ({
      id: nodeItem.id,
      hostname: nodeItem.hostname,
      count: topo.gpus.filter(
        (gpu) => gpu.node_id === nodeItem.id && store.allSelectedUuids.value.has(gpu.uuid),
      ).length,
    }))
    .filter((item) => item.count > 0)
})

/** 可作 CPU 部署落点的节点(引擎声明 cpu + 在线;llama.cpp 下即全部在线节点) */
const cpuCandidateNodes = computed(() =>
  (store.topology.value?.nodes ?? []).filter((nodeItem) => store.isCpuNodeSelectable(nodeItem)),
)

/**
 * 加载完成且确实无任何落点:无节点,或有节点但既无 GPU 也无可作 CPU 部署的在线节点。
 * 有 CPU 落点的纯 CPU 集群不算空——节点卡片是纯 CPU 部署入口,
 * 空态覆盖层会把它们盖住导致无法选择(与 noneSelectable 的 CPU 退路口径一致)。
 */
const isEmpty = computed(
  () =>
    !store.topologyLoading.value &&
    !store.topologyError.value &&
    !!store.topology.value &&
    (!store.topology.value.nodes.length ||
      (!store.topology.value.gpus.length && cpuCandidateNodes.value.length === 0)),
)

/** 空态文案:无节点 vs 有节点但无 GPU 且无 CPU 落点(引擎未声明 cpu 或节点全离线) */
const emptyDescription = computed(() => {
  if (!store.topology.value?.nodes.length) return '该集群暂无节点'
  return store.engineSupportsCpu.value
    ? '该集群暂无 GPU,且无在线节点可作 CPU 部署'
    : '该集群暂无 GPU,当前引擎不支持 CPU 部署'
})

/** CPU 部署已钉份数(摘要区「已钉 N / 总份数」口径) */
const cpuPinnedCount = computed(
  () => store.cpuNodeSelections.value.filter((nodeId) => nodeId !== null).length,
)

/**
 * CPU 部署钉定分组摘要:按节点聚合份数(tag 形态展示;关闭 tag = 移除该节点全部钉定)。
 * 同节点可承载多份(容量由后端按份记账,前端不设上限)。
 */
const cpuSummaryGroups = computed(() => {
  const topo = store.topology.value
  if (!topo) return []
  const counts = new Map<string, number>()
  for (const nodeId of store.cpuNodeSelections.value) {
    if (!nodeId) continue
    counts.set(nodeId, (counts.get(nodeId) ?? 0) + 1)
  }
  return topo.nodes
    .map((nodeItem) => ({
      id: nodeItem.id,
      hostname: nodeItem.hostname,
      count: counts.get(nodeItem.id) ?? 0,
    }))
    .filter((item) => item.count > 0)
})

/**
 * 纯 CPU 部署每份内存需求(逐层表 0 档 ram_gb;cpu_only 口径已含落内存的 KV,不加 kv_gb)。
 * 未知(模型缺形状元数据/预检未带 allow_cpu)→ null 不展示。
 */
const cpuRamNeedGb = computed(() => store.inspect.value?.memory?.tiers[0]?.ram_gb ?? null)

/** 有 GPU 但全集群无可选(离线/已占用/架构不匹配/无匹配镜像);不覆盖画布,逐卡禁选原因仍可查看 */
const noneSelectable = computed(
  () =>
    !store.topologyLoading.value &&
    !store.topologyError.value &&
    !!store.topology.value &&
    store.topology.value.gpus.length > 0 &&
    selectableCount.value === 0 &&
    // 该集群有 CPU 落点节点时仍可继续(纯 CPU 部署)
    cpuCandidateNodes.value.length === 0,
)


const canNext = computed(
  () =>
    (selectedTotal.value > 0 || store.cpuMode.value) &&
    !topologyRuleError.value &&
    // 纯 CPU 档空落点(钉定节点内存不足/多副本挤不下):后端已给出容量阻断,不得推进
    !(store.cpuMode.value && store.noPlacementReason.value),
)

/** 未选任何落点时的提示:llama.cpp 主入口是节点卡片(纯 CPU),其余引擎是选卡 */
const noSelectionTip = computed(() =>
  isLlamaCpp.value ? '请点一个节点卡片走 CPU 部署,或至少选择一张 GPU' : '请至少选择一张 GPU(或一个 CPU 节点)',
)

/** 「下一步」禁用提示:与下方 validate 兜底提示一致(未选优先,其次 CPU 空落点/跨机约束) */
const nextDisabledTip = computed(() => {
  if (!selectedTotal.value && !store.cpuMode.value) return noSelectionTip.value
  if (store.cpuMode.value && store.noPlacementReason.value) return store.noPlacementReason.value
  return topologyRuleError.value ?? undefined
})

/**
 * 引擎/模型不匹配提示(格式/类别):早提示,避免选完卡才在预检报错。
 * 事实源是能力字典(引擎 supported_formats/supported_categories),不写引擎名。
 */
const engineMismatchTip = computed(() => {
  if (store.engineServesModel.value) return ''
  const engine = store.engines.value.find((item) => item.engine === store.effectiveEngine.value)
  const model = store.selectedModel.value
  if (!engine || !model) return ''
  const mismatches: string[] = []
  if (model.format && !(engine.supported_formats ?? []).includes(model.format))
    mismatches.push(`格式 ${model.format}`)
  if (model.model_type && !(engine.supported_categories ?? []).includes(model.model_type))
    mismatches.push(`类别 ${model.model_type}`)
  if (!mismatches.length) return ''
  return (
    `当前引擎 ${engine.display_name} 不支持该模型的${mismatches.join('、')}` +
    `(该引擎支持 格式 ${(engine.supported_formats ?? []).join('/')}、类别 ${(engine.supported_categories ?? []).join('/')});` +
    '请更换引擎或改用匹配的模型'
  )
})

/** 预检推荐摘要:纯 CPU 档展示内存需求与节点落点,显存档展示显存需求 + 推荐引擎/拓扑(基础预检结果) */const recommendSummary = computed(() => {
  const result = store.inspect.value
  if (!result) return ''
  const parts: string[] = []
  const memory = result.memory
  const cpuOnly = memory?.scenario === 'cpu_only'
  if (cpuOnly) {
    // 纯 CPU:卡口径(显存/tp)不适用;需求取逐层表 0 档,落点为后端 binpack 节点集
    const ram = memory.tiers[0]?.ram_gb
    if (ram != null) parts.push(`CPU 部署 · 需约 ${ram}GB 内存`)
    const nodeCount = result.recommended_placement?.node_ids?.length ?? 0
    if (nodeCount) parts.push(`推荐落点 ${nodeCount} 个节点`)
  } else {
    const required = result.cluster_gpus[0]?.required_vram_gb
    if (required) parts.push(`模型约需显存 ${required}GB`)
  }
  const recommendation = result.recommendation
  if (recommendation) {
    parts.push(`推荐引擎 ${store.engineLabel(recommendation.engine)}`)
    if (!cpuOnly)
      parts.push(`推荐 tp=${recommendation.topology.tp}(每卡约 ${recommendation.vram_per_gpu_gb}GB)`)
  }
  return parts.join(' · ')
})

/** 最大上下文输入提示:留空即按 inspect 推荐值部署(派生拓扑回退同口径),推荐值直接亮在 placeholder */
const maxModelLenPlaceholder = computed(() => {
  const recommended = store.inspect.value?.recommendation?.topology.max_model_len
  return recommended ? `留空按推荐 ${recommended}` : '留空按模型推荐'
})

/** 最大上下文快捷档:展示文案 → 实际 token 数(下拉选择,变更走同一条防抖重检链路) */
const MAX_MODEL_LEN_PRESETS = [
  { label: '8K', value: 8 * 1024 },
  { label: '16K', value: 16 * 1024 },
  { label: '32K', value: 32 * 1024 },
  { label: '64K', value: 64 * 1024 },
  { label: '128K', value: 128 * 1024 },
  { label: '256K', value: 256 * 1024 },
  { label: '1M', value: 1024 * 1024 },
]

/** 快捷档下拉选项:首项「自动」回退推荐(null);档位 label 带实际 token 数;当前命中项加 ✓ 标记 */
const maxModelLenPresetOptions = computed(() => [
  {
    label: `${store.form.maxModelLen === null ? '✓ ' : ''}自动(留空按推荐)`,
    key: 'auto',
  },
  ...MAX_MODEL_LEN_PRESETS.map((preset) => ({
    label: `${preset.value === store.form.maxModelLen ? '✓ ' : ''}${preset.label}(${preset.value})`,
    key: String(preset.value),
  })),
])

function onMaxModelLenPresetSelect(key: string) {
  store.form.maxModelLen = key === 'auto' ? null : Number(key)
}

/** 高级参数入口是否渲染:有 KV 精度选项且模型确实有 KV cache 时才显示(部署规格行尾按钮 + Popover) */
const showAdvanced = computed(() => hasKvCache.value && kvCacheOptions.value.length > 0)

/** 「高级」触发器文案:手动选过非默认档位时带出档名(按钮同步亮主色),收起状态也能一眼看到动过 */
const advancedTriggerLabel = computed(() =>
  store.form.kvCacheDtype ? `高级 · ${kvCacheOption.value?.label ?? store.form.kvCacheDtype}` : '高级',
)

/** 引擎切换后旧档位在新引擎词汇里不存在时回退默认档,避免下拉空白与残留 flag */
watch(kvCacheOptions, (options) => {
  if (store.form.kvCacheDtype && !options.some((item) => item.value === store.form.kvCacheDtype)) {
    store.form.kvCacheDtype = ''
  }
})

/**
 * 切入须单机语境(llamacpp / 引擎或镜像版本无多机 / cann 选卡):
 * 残留的跨节点选集收敛到单个节点(每份各自收敛;llama 按部署级锁全部署对齐)
 */
watch(
  () => store.crossNodePpBlocked.value,
  (blocked) => {
    if (blocked) store.narrowSelectionToSingleNode()
  },
)

/**
 * 部署规格变更(副本数/最大上下文/KV 精度):防抖强制重检。
 * 份数参与逐份规划、上下文与 KV 精度影响显存评估,旧 inspect 会给出过期落点;已选卡保留不清空,
 * 由拓扑约束实时重推导,凑不齐时禁用下一步。
 */
let specReloadTimer: ReturnType<typeof setTimeout> | null = null

async function runSpecReload() {
  specReloadTimer = null
  await store.loadInspect(true)
  // 重检后把推荐落点补齐到空份(新增副本自动预选;已手选的份不覆盖,plan 卡被占用则跳过)
  if (!store.inspectError.value) {
    store.applyRecommendedSelection()
  }
}

function scheduleSpecReload() {
  if (specReloadTimer) clearTimeout(specReloadTimer)
  specReloadTimer = setTimeout(runSpecReload, 300)
}

watch(
  // CPU 逐份钉定集也进监听:任一份落点变化都要按新拓扑重检(可用内存/是否装得下/逐份规划)
  () => [
    store.form.replicas,
    store.form.maxModelLen,
    store.form.kvCacheDtype,
    store.cpuNodeSelections.value.join(','),
  ] as const,
  () => scheduleSpecReload(),
)

// 卸载清掉挂起的防抖:避免迟到回调在已离开本步后触发重检与预选
onUnmounted(() => {
  if (specReloadTimer) clearTimeout(specReloadTimer)
})

defineExpose({
  canNext,
  nextDisabledTip,
  /** 点击时的兜底门控(canNext 已做按钮禁用) */
  validate: () => {
    if (!selectedTotal.value && !store.cpuMode.value) {
      message.warning(noSelectionTip.value)
      return Promise.resolve(false)
    }
    // 纯 CPU 档空落点:提示容量阻断原因(后端 issues 文案)
    if (store.cpuMode.value && store.noPlacementReason.value) {
      message.warning(store.noPlacementReason.value)
      return Promise.resolve(false)
    }
    if (topologyRuleError.value) {
      message.warning(topologyRuleError.value)
      return Promise.resolve(false)
    }
    return Promise.resolve(true)
  },
})
</script>

<template>
  <div class="flex flex-col gap-[1.2rem] h-full">
    <!-- 预检反馈:失败可降级为无评估选卡(创建前仍会全量校验);成功展示推荐摘要 -->
    <LmAlert v-if="inspectError" type="warning">
      <div class="flex items-center justify-between gap-[1rem]">
        <span>部署预检加载失败,可继续选卡,下一步会重新校验</span>
        <n-button size="tiny" quaternary @click="loadInspect(true)"> 重试 </n-button>
      </div>
    </LmAlert>
    <!-- 引擎不支持该模型(格式/类别):能力字典视角早提示 -->
    <LmAlert v-else-if="engineMismatchTip" type="warning">
      {{ engineMismatchTip }}
    </LmAlert>
    <LmAlert v-else-if="noneSelectable" type="error">
      当前集群所有 GPU
      均不可选(离线/架构不匹配/无匹配镜像),且无可用 CPU 落点节点,无法继续部署;请更换目标集群或释放资源后重试
    </LmAlert>
    <!-- 推荐落点为空:副本数/并行度超出集群空闲资源(后端 issues 分流文案);纯 CPU 档空落点=内存容量阻断,无默认预选须显式报错 -->
    <LmAlert v-else-if="store.noPlacementReason.value" type="error">
      {{ store.noPlacementReason.value }}
    </LmAlert>
    <LmAlert v-else-if="recommendSummary" :show-icon="false" type="primary">
      {{ recommendSummary }}
    </LmAlert>

    <!-- 部署规格:副本卡片(切换编辑目标/增删份)直接驱动选卡,变更防抖重检 -->
    <div class="flex items-center gap-[1.2rem] flex-wrap">
      <div class="flex items-center gap-[.6rem] shrink-0">
        <span class="text-base text-secondary shrink-0">最大上下文</span>
        <n-input-number
          v-model:value="store.form.maxModelLen"
          :min="1"
          :show-button="false"
          size="small"
          class="w-[11rem]"
          :placeholder="maxModelLenPlaceholder"
        >
          <!-- 快捷档下拉合入输入框 suffix:点箭头展开,选中写入对应 token 数(走同一条防抖重检链路) -->
          <template #suffix>
            <n-dropdown
              trigger="click"
              :options="maxModelLenPresetOptions"
              @select="onMaxModelLenPresetSelect"
            >
              <n-icon class="cursor-pointer text-secondary">
                <ChevronDownOutline />
              </n-icon>
            </n-dropdown>
          </template>
        </n-input-number>
      </div>
      <div class="flex items-center gap-[.6rem] flex-wrap min-w-0">
        <span class="text-base text-secondary shrink-0">副本</span>
        <div
          v-for="card in replicaCards"
          :key="card.seq"
          class="replica-card group flex items-center gap-[.6rem] rounded-md border py-[.4rem] pl-[.9rem] pr-[.9rem] cursor-pointer transition-colors"
          :class="card.active
            ? 'border-primary bg-primary-soft'
            : 'border-line-strong bg-fill-1 hover:border-primary'"
          role="button"
          tabindex="0"
          :aria-pressed="card.active"
          @click="store.setActiveReplica(card.index)"
          @keydown.enter.prevent="store.setActiveReplica(card.index)"
          @keydown.space.prevent="store.setActiveReplica(card.index)"
        >
          <span
            class="w-[1rem] h-[1rem] rounded-full shrink-0"
            :style="{ backgroundColor: card.color }"
          />
          <div class="flex flex-col">
            <span
              class="text-base leading-[1.3]"
              :class="card.active ? 'text-primary font-medium' : 'text-default'"
            >
              副本{{ card.seq }}
            </span>
            <span class="text-xs leading-[1.3] text-secondary whitespace-nowrap">
              {{ card.summary }}
            </span>
          </div>
          <!-- 移除副本(至少保留 1 份;份内有卡时二次确认):可聚焦按钮 + aria-label,
               触屏/键盘可达(hover 才显形的纯图标在移动端无 hover,份数会无法减少) -->
          <n-button
            v-if="replicaCards.length > 1"
            size="tiny"
            quaternary
            circle
            class="!min-w-[2.2rem] !min-h-[2.2rem]"
            aria-label="移除该副本"
            @click.stop="onRemoveReplica(card.index)"
          >
            <template #icon>
              <n-icon><CloseOutline /></n-icon>
            </template>
          </n-button>
        </div>
        <n-button
          v-if="replicaCards.length < REPLICA_COUNT_MAX"
          size="small"
          @click="onAddReplica"
        >
          <template #icon>
            <n-icon><AddOutline /></n-icon>
          </template>
          添加副本
        </n-button>
      </div>

      <!-- 跨节点选卡形态提示:亮出将提交的拓扑形态(PP 可用=节点内 TP + 跨机 PP,否则跨机 TP) -->
      <span
        v-if="store.crossNodeModeTip.value"
        class="text-base text-primary shrink-0"
      >
        {{ store.crossNodeModeTip.value }}
      </span>

      <!--
        高级参数(KV Cache 精度):规格行尾按钮 + Popover 承载——不占专用行,展开不挤压画布(浮层不参与布局)。
        选项表由后端按引擎给出(引擎原生档位),模型无 KV cache(embedding/rerank)或引擎不声明能力时整块不渲染。
      -->
      <n-popover
        v-if="showAdvanced"
        trigger="click"
        placement="bottom"
      >
        <template #trigger>
          <n-button
            size="small"
            quaternary
            :type="store.form.kvCacheDtype ? 'primary' : 'default'"
            class="shrink-0"
            aria-label="高级参数"
          >
            <template #icon>
              <n-icon><SettingsOutline /></n-icon>
            </template>
            {{ advancedTriggerLabel }}
          </n-button>
        </template>
        <div class="flex flex-col gap-[.8rem] w-[30rem]">
          <div class="flex items-center gap-[.6rem]">
            <span class="text-base text-secondary shrink-0">KV Cache 精度</span>
            <!-- 菜单宽度放开跟随内容:默认与输入框同宽,长档位文案(如「FP8 E4M3(KV 约 0.5×)」)会被省略号截断 -->
            <LmSelect
              v-model:value="store.form.kvCacheDtype"
              :options="kvCacheSelectOptions"
              size="small"
              class="flex-1"
              aria-label="KV Cache 精度"
            />
          </div>
          <!-- 选项说明取当前档位(默认档也有文案);量化 KV 属高级项,异常时回退默认档 -->
          <p
            v-if="kvCacheOption?.hint"
            class="text-base text-secondary"
          >
            {{ kvCacheOption.hint }}
          </p>
        </div>
      </n-popover>
    </div>

    <div class="flex items-center justify-between gap-[1.6rem] flex-wrap">
      <!-- 框选/编辑目标提示:框选说明平移/缩放方式;常规多副本说明选卡落点 -->
      <span v-if="batchMode" class="text-base text-warning">
        拖拽框选批量加入副本{{ activeReplicaSeq }};按住空格或鼠标中键拖动平移视图,滚轮缩放
      </span>
      <span
        v-else-if="store.cpuMode.value && replicaCards.length > 1"
        class="text-base text-secondary"
      >
        正在为<span class="text-primary font-medium"> 副本{{ activeReplicaSeq }} </span>钉定
        CPU 落点;点节点卡片给该副本选节点,点已钉节点取消
      </span>
      <span
        v-else-if="!store.cpuMode.value && replicaCards.length > 1"
        class="text-base text-secondary"
      >
        正在为<span class="text-primary font-medium"> 副本{{ activeReplicaSeq }} </span>选卡;点
        GPU 卡/节点卡片加入该副本,点已选卡取消
      </span>
      <!-- ml-auto:无左侧文案时按钮保持靠右 -->
      <div class="ml-auto flex items-center gap-[1.2rem]">
        <!-- 多副本下「全选」升级为按桶贪心自动分配各份;单副本保持全选语义 -->
        <n-button
          v-show="showSelectActions"
          size="small"
          quaternary
          :disabled="!batchSelectableCount && replicaCards.length === 1"
          :title="replicaCards.length > 1 ? '全部可选卡按节点/架构自动分配到各副本' : '选中全部可选 GPU'"
          @click="selectAll()"
        >
          <template #icon>
            <n-icon><CheckboxOutline /></n-icon>
          </template>
          {{ replicaCards.length > 1 ? '自动分配' : '全选' }}
        </n-button>
        <!-- 反选:激活副本在可选集合内取反(被占/离线/其它副本的卡始终不参与) -->
        <n-button
          v-show="showSelectActions"
          size="small"
          quaternary
          :disabled="!store.selectedGpuUuids.value.size && !batchSelectableCount"
          @click="invertSelection()"
        >
          <template #icon>
            <n-icon><InvertModeOutline /></n-icon>
          </template>
          反选
        </n-button>
        <n-button
          size="small"
          quaternary
          :disabled="!selectedTotal"
          @click="clearSelection()"
        >
          清空
        </n-button>
        <n-button
          size="small"
          :quaternary="!batchMode"
          :secondary="batchMode"
          :type="batchMode ? 'primary' : 'default'"
          @click="toggleBatchMode"
        >
          <template #icon>
            <n-icon><ScanOutline /></n-icon>
          </template>
          框选模式
        </n-button>
      </div>
    </div>

    <!-- 画布占满剩余高度;min-h 下限保证窄屏/多副本(上方规格行换行)时画布不被压没,
         容器(向导第二步列)以 overflow-y-auto 兜底产生页面内滚动而非画布 0 高度 -->
    <div class="relative flex-1 min-h-[24rem] rounded-lg border border-line overflow-hidden">
      <VueFlow
        :nodes="graph.nodes"
        :edges="graph.edges"
        class="workflow-flow bg-fill-2"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :min-zoom="0.2"
        :max-zoom="1.5"
        :elements-selectable="batchMode"
        :selection-key-code="batchMode ? true : null"
        :pan-on-drag="panOnDrag"
        :selection-mode="SelectionMode.Partial"
      >
        <Background :gap="24" />
        <Controls position="bottom-left" />
        <template #node-cluster="nodeProps">
          <SelectClusterCard v-bind="nodeProps" />
        </template>
        <template #node-node="nodeProps">
          <SelectNodeCard v-bind="nodeProps" />
        </template>
        <template #node-gpu="nodeProps">
          <SelectGpuCard v-bind="nodeProps" />
        </template>
      </VueFlow>
      <!-- 加载/失败/空态覆盖层:Vue Flow 保持挂载,避免 useVueFlow 实例失效 -->
      <div
        v-if="topologyLoading"
        class="absolute inset-0 z-10 flex items-center justify-center bg-fill-2"
      >
        <n-spin size="large" />
      </div>
      <div
        v-else-if="topologyError"
        class="absolute inset-0 z-10 flex items-center justify-center bg-fill-2"
      >
        <n-result status="error" title="拓扑加载失败" description="请检查网络后重试">
          <template #footer>
            <n-button type="primary" @click="loadTopology(true)"> 重试 </n-button>
          </template>
        </n-result>
      </div>
      <div
        v-else-if="isEmpty"
        class="absolute inset-0 z-10 flex items-center justify-center bg-fill-2"
      >
        <n-empty :description="emptyDescription" />
      </div>
    </div>

    <!-- 已选摘要:纯 CPU 显示逐份钉定分组(host × N 份)+ 每份内存需求 + 未齐份数警告;显存部署显示总量 + 按节点分组 -->
    <div v-if="store.cpuMode.value" class="flex items-center gap-[.8rem] flex-wrap">
      <span class="text-base text-default shrink-0">
        CPU 部署节点
        <span class="text-primary font-semibold">{{ cpuPinnedCount }}</span>
        / {{ replicaCards.length }} 份已钉
      </span>
      <template v-if="cpuSummaryGroups.length">
        <span class="text-base text-secondary shrink-0">分布:</span>
        <n-tag
          v-for="group in cpuSummaryGroups"
          :key="group.id"
          size="small"
          :bordered="false"
          closable
          @close="store.removeCpuNodeSelection(group.id)"
        >
          {{ group.hostname }} × {{ group.count }} 份
        </n-tag>
      </template>
      <span v-if="cpuRamNeedGb !== null" class="text-base text-secondary shrink-0">
        每份需约 {{ cpuRamNeedGb }}GB 内存
      </span>
      <!-- 副本未钉齐:topologyRuleError 同步禁用下一步(此处提示用户补齐) -->
      <span v-if="topologyRuleError" class="text-base text-warning shrink-0">
        {{ topologyRuleError }}
      </span>
    </div>
    <div v-else class="flex items-center gap-[.8rem] flex-wrap">
      <!-- llama.cpp:节点卡片点击即走纯 CPU 部署(默认不预选卡),提示两条入口 -->
      <span v-if="isLlamaCpp && !selectedTotal" class="text-base text-secondary shrink-0">
        点节点卡片 = 纯 CPU 部署(不占卡);点 GPU 卡 = 显存部署
      </span>
      <span class="text-base text-default shrink-0">
        已选
        <span class="text-primary font-semibold">{{ selectedTotal }}</span>
        张 GPU
        <!-- 各份等量口径:每副本 M 张 × R 份;不等量时不展示(由规则提示调整) -->
        <template v-if="replicaCards.length > 1 && perCopyGpus >= 1">
          / 每副本 {{ perCopyGpus }} 张 × {{ replicaCards.length }} 份
        </template>
      </span>
      <template v-if="selectedGroups.length">
        <span class="text-base text-secondary shrink-0">分布:</span>
        <n-tag
          v-for="group in selectedGroups"
          :key="group.id"
          size="small"
          :bordered="false"
          closable
          @close="store.removeNodeSelection(group.id)"
        >
          {{ group.hostname }} × {{ group.count }}
        </n-tag>
      </template>
      <!-- 副本约束不合法:提示份未选满/卡数不一致/跨机约束(canNext 同步禁用下一步) -->
      <span v-if="topologyRuleError" class="text-base text-warning shrink-0">
        {{ topologyRuleError }}
      </span>
    </div>
  </div>
</template>

<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
</style>

<style lang="scss">
/* 画布配色:全部走主题变量,明暗自适应;scoped 无法命中 Vue Flow 内部 DOM,故用全局块 + 前缀限定 */
.workflow-flow {
  .vue-flow__edge.edge-static .vue-flow__edge-path {
    stroke: var(--c-line-strong);
  }
  .vue-flow__controls {
    box-shadow: none;
    border: 1px solid var(--c-line-strong);
  }
  .vue-flow__controls-button {
    background: var(--c-fill-1);
    border-bottom: 1px solid var(--c-line);
    fill: var(--c-default);
  }
  /* 框选选区:默认主题是硬编码蓝,改走主题色,明暗自适应 */
  .vue-flow__selection,
  .vue-flow__nodesselection-rect {
    background: rgba(var(--n-primary-color), 0.08);
    border: 1px dashed rgba(var(--n-primary-color), 0.6);
  }
}
</style>
