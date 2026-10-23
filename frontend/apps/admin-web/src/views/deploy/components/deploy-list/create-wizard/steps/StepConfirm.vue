<script setup lang="ts">
import { message } from '@repo/hooks'
import { AddOutline, CloseOutline } from '@vicons/ionicons5'
import { modelTagOptions, modelTypeOptions } from '@/views/model-release/useStore'
import { useWizardStore } from '../useStore'

const store = useWizardStore()
// 模板里使用的成员必须解构为顶层 ref(store 是普通对象,嵌套 ref 在模板不会自动解包)
const { editArgs, editEnv } = store

/** 引擎展示文案:auto 时附 inspect 推荐结果 */
const engineLabel = computed(() => {
  const base = store.engineLabel(store.form.engine)
  if (store.form.engine !== 'auto') return base
  const recommended = store.inspect.value?.recommendation?.engine
  if (!recommended) return base
  return `${base}(推荐:${store.engineLabel(recommended)})`
})
/** 集群名:优先下拉选项的原始数据,兜底拓扑数据 */
const clusterName = computed(
  () =>
    store.clusterOptions.value.find((item) => item.value === store.form.clusterId)?.raw.name ??
    store.topology.value?.cluster.name ??
    '--',
)
/** 模型名:按 modelId 从下拉选项回显 */
const modelName = computed(
  () => store.modelOptions.value.find((item) => item.value === store.form.modelId)?.label ?? '--',
)

/** 拓扑摘要(units/copies 归一口径):pp>1 = 节点内 TP + 跨机 PP;跨机 pp=1 标注跨机 TP */
const topologyLabel = computed(() => {
  const topo = store.derivedTopology.value
  if (!topo) return '--'
  if (store.cpuMode.value) {
    return `纯 CPU · 每副本 1 实例 × ${topo.replicas} 份`
  }
  const perCopy = topo.tp * topo.pp
  const total = perCopy * topo.replicas
  if (topo.nnodes > 1) {
    const mode = topo.pp > 1
      ? `每副本跨 ${topo.nnodes} 节点(节点内 TP + 跨机 PP)`
      : `每副本跨 ${topo.nnodes} 节点(跨机 TP)`
    return `tp=${topo.tp}${topo.pp > 1 ? ` · pp=${topo.pp}` : ''} · 每副本 ${perCopy} 张 × ${topo.replicas} 份 = ${total} 张 · ${mode}`
  }
  return `tp=${topo.tp}${topo.pp > 1 ? ` · pp=${topo.pp}` : ''} · 每副本 ${perCopy} 张 × ${topo.replicas} 份 = ${total} 张 · 单机`
})
const maxModelLen = computed(() => store.derivedTopology.value?.max_model_len ?? '--')

/** CPU 部署逐份落点明细(纯 CPU 部署:算力明细区逐份展示节点与可用内存) */
const cpuReplicaRows = computed(() => {
  if (!store.cpuMode.value) return []
  const topo = store.topology.value
  return store.cpuNodeSelections.value.map((nodeId, index) => {
    const node = nodeId ? topo?.nodes.find((item) => item.id === nodeId) : null
    const support = nodeId ? store.nodeSupportMap.value.get(nodeId) : undefined
    return {
      seq: index + 1,
      hostname: nodeId ? (node?.hostname ?? nodeId) : '--',
      availableMemoryGb: support?.available_memory_gb ?? null,
    }
  })
})

/** 已选 GPU 明细:多副本按配对份分组(份内同节点同架构),单副本按节点分组 */
const gpuGroups = computed(() => {
  const topo = store.topology.value
  if (!topo) return []
  const pairs = store.pairingPreview.value.pairs
  if (pairs.length) {
    // 多副本:每份一组,组标题带份序号与架构(份间可异构)
    const gpuByUuid = new Map(topo.gpus.map((gpu) => [gpu.uuid, gpu]))
    const hostnames = new Map(topo.nodes.map((node) => [node.id, node.hostname] as const))
    return pairs
      .map((pair) => ({
        hostname: `副本${pair.seq} · ${hostnames.get(pair.nodeIds[0] ?? '') ?? pair.nodeIds[0] ?? '--'} · ${pair.arch}`,
        gpus: pair.gpuUuids
          .map((uuid) => gpuByUuid.get(uuid))
          .filter((gpu): gpu is NonNullable<typeof gpu> => Boolean(gpu)),
      }))
      .filter((item) => item.gpus.length > 0)
  }
  return topo.nodes
    .map((nodeItem) => ({
      hostname: nodeItem.hostname,
      gpus: topo.gpus.filter(
        (gpu) => gpu.node_id === nodeItem.id && store.selectedGpuUuids.value.has(gpu.uuid),
      ),
    }))
    .filter((item) => item.gpus.length > 0)
})
const selectedCount = computed(() => store.allSelectedUuids.value.size)

/** 预检结果(Step2→3 全量预检后必为最新):issues 为告警,deployable 已在推进时门控 */
const inspectResult = computed(() => store.inspect.value)

/**
 * 异构份差异表(仅逐份规划存在且架构不一致时非 null):
 * 每份一栏展示镜像/单卡余量,跨份同值字段标 = 、差异字段标 ★(异构份镜像随架构各选变体)。
 */
const replicaDiff = computed(() => {
  const placement = inspectResult.value?.recommended_placement
  const plans = placement?.replica_plans ?? []
  if (plans.length < 2 || new Set(plans.map((plan) => plan.arch)).size <= 1) return null
  const unitPlans = placement?.unit_plans ?? []
  const hostnames = new Map(
    (store.topology.value?.nodes ?? []).map((node) => [node.id, node.hostname] as const),
  )
  const rows = plans.map((plan) => {
    const unit = plan.units[0]
    const unitPlan = unitPlans.find((item) => item.role === unit?.role)
    return {
      seq: plan.seq,
      arch: plan.arch,
      hostname: hostnames.get(plan.node_ids[0] ?? '') ?? plan.node_ids[0] ?? '--',
      fit: plan.fit,
      image: unitPlan?.image ?? '--',
      headroomGb: unit?.headroom_gb ?? null,
    }
  })
  return {
    rows,
    differs: {
      image: new Set(rows.map((row) => row.image)).size > 1,
      headroomGb: new Set(rows.map((row) => row.headroomGb)).size > 1,
    },
  }
})

/** 参数行 AutoComplete 选项:空输入出全部推荐,输入后按包含过滤(不区分大小写) */
function argOptions(value: string) {
  const keyword = value.trim().toLowerCase()
  if (!keyword) return store.argSuggestionOptions.value
  return store.argSuggestionOptions.value.filter((option) =>
    option.value.toLowerCase().includes(keyword),
  )
}

defineExpose({
  /** 确认部署:提交创建;成功后由向导控制器退出,失败停留本步可重试 */
  validate: async () => {
    try {
      await store.submitCreate()
    } catch {
      return false
    }
    message.success(`部署 ${store.form.name} 已创建`)
    return true
  },
})
</script>

<template>
  <div class="flex flex-col gap-[1.6rem] w-full max-w-[56rem] mx-auto">
    <n-descriptions label-placement="left" bordered :column="2" size="small">
      <n-descriptions-item label="部署名称">
        {{ store.form.name }}
      </n-descriptions-item>
      <n-descriptions-item label="模型">
        {{ modelName }}
      </n-descriptions-item>
      <n-descriptions-item label="推理引擎">
        {{ engineLabel }}
      </n-descriptions-item>
      <n-descriptions-item label="目标集群">
        {{ clusterName }}
      </n-descriptions-item>
      <n-descriptions-item label="GPU 数量">
        {{ store.cpuMode.value ? '0 张(CPU 部署)' : `${selectedCount} 张` }}
      </n-descriptions-item>
      <n-descriptions-item label="并行拓扑">
        {{ topologyLabel }}
      </n-descriptions-item>
      <n-descriptions-item label="最大上下文">
        {{ maxModelLen }}
      </n-descriptions-item>
    </n-descriptions>

    <!-- 发布配置:部署完成后自动发布对外路由(对外名/定价/标签/描述,空值跟随模型);详情默认收起,与高级参数一致 -->
    <div class="flex flex-col gap-[.8rem]">
      <div class="flex items-center justify-between">
        <span class="text-strong font-semibold">发布配置</span>
        <div class="flex items-center gap-[.6rem]">
          <span class="text-small text-secondary">部署完成后自动发布</span>
          <n-switch v-model:value="store.publish.autoPublish" size="small" />
        </div>
      </div>
      <div class="rounded-[1.2rem] border border-line bg-fill-1 px-[1.6rem] py-[1.2rem]">
        <n-collapse>
          <n-collapse-item title="发布详情" name="publish">
            <div
              v-if="store.publish.autoPublish"
              class="grid grid-cols-2 gap-x-[1.6rem] gap-y-[1rem] pt-[.4rem]"
            >
              <div class="flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">对外服务名</span>
                <n-input
                  v-model:value="store.publish.name"
                  :placeholder="`跟随模型名(${modelName})`"
                  :maxlength="255"
                />
              </div>
              <div class="flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">模型类别</span>
                <LmSelect
                  v-model:value="store.publish.modelType"
                  :options="modelTypeOptions"
                  clearable
                  placeholder="跟随模型"
                />
              </div>
              <div class="col-span-2 flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">模型描述</span>
                <n-input
                  v-model:value="store.publish.description"
                  type="textarea"
                  :rows="2"
                  placeholder="模型中心展示,可空"
                />
              </div>
              <div class="col-span-2 flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">能力标签</span>
                <LmSelect
                  v-model:value="store.publish.tags"
                  :options="modelTagOptions"
                  multiple
                  placeholder="按能力勾选,可空"
                />
              </div>
              <div class="flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">输入定价(元/百万token)</span>
                <n-input-number
                  v-model:value="store.publish.inputPrice"
                  :min="0"
                  clearable
                  placeholder="空=不计费"
                  class="w-full"
                />
              </div>
              <div class="flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">输出定价(元/百万token)</span>
                <n-input-number
                  v-model:value="store.publish.outputPrice"
                  :min="0"
                  clearable
                  placeholder="空=不计费"
                  class="w-full"
                />
              </div>
              <div class="col-span-2 flex flex-col gap-[.4rem]">
                <span class="text-small text-secondary">缓存命中输入定价(元/百万token)</span>
                <n-input-number
                  v-model:value="store.publish.cachedInputPrice"
                  :min="0"
                  clearable
                  placeholder="空=不计费"
                  class="w-full"
                />
              </div>
            </div>
            <div v-else class="text-base text-secondary pt-[.4rem]">
              已关闭自动发布:部署达成后仅注册推理端点,不创建对外路由与绑定;可稍后在发布中心手动发布
            </div>
          </n-collapse-item>
        </n-collapse>
      </div>
    </div>

    <!-- 算力明细 + 高级参数:同一边框容器,横向分割线隔开;高级参数默认收起,展开限高内部滚动 -->
    <div class="flex flex-col gap-[.8rem]">
      <span class="text-strong font-semibold">算力明细</span>
      <div class="rounded-[1.2rem] border border-line bg-fill-1 px-[1.6rem] py-[1.2rem]">
        <!-- CPU 部署:无 GPU,逐份展示落点节点与可用内存(多份可分布不同节点) -->
        <div
          v-if="cpuReplicaRows.length"
          class="flex flex-col gap-[.8rem]"
        >
          <div v-for="row in cpuReplicaRows" :key="row.seq" class="flex flex-wrap items-center gap-[.8rem]">
            <span class="text-small text-strong font-medium">
              CPU 部署 · 副本{{ row.seq }} · {{ row.hostname }}
            </span>
            <n-tag size="small" :bordered="false"> 纯 CPU </n-tag>
            <n-tag v-if="row.availableMemoryGb !== null" size="small" :bordered="false">
              可用内存 {{ row.availableMemoryGb }}GB
            </n-tag>
          </div>
        </div>
        <!-- 已选 GPU:多副本按配对份分组(份标题带序号/节点/架构),单副本按节点分组 -->
        <div
          v-else
          class="flex flex-col gap-[1.2rem]"
        >
          <div v-for="group in gpuGroups" :key="group.hostname">
            <div class="text-small text-strong font-medium mb-[.8rem]">
              {{ group.hostname }}
            </div>
            <div class="flex flex-wrap gap-[.8rem]">
              <n-tag v-for="gpu in group.gpus" :key="gpu.uuid" size="small" :bordered="false">
                {{ gpu.name }} · GPU {{ gpu.index }}
              </n-tag>
            </div>
          </div>
        </div>

        <!-- 异构份差异:各份架构不同时分栏对照,跨份同值标 = 差值标 ★ -->
        <template v-if="replicaDiff">
          <div class="border-t border-line my-[1.2rem]" />
          <div class="text-small text-strong font-medium mb-[.8rem]">
            异构份对照(各份架构不同,镜像按架构各选变体)
          </div>
          <div class="grid grid-cols-2 gap-[1.2rem]">
            <div
              v-for="row in replicaDiff.rows"
              :key="row.seq"
              class="rounded-[1.2rem] border border-line bg-fill-2 px-[1.2rem] py-[.8rem] flex flex-col gap-[.4rem]"
            >
              <div class="text-small text-strong font-medium">
                r{{ row.seq }} · {{ row.arch }} @ {{ row.hostname }}
                <span v-if="!row.fit" class="text-warning">(不满足)</span>
              </div>
              <div class="text-small text-secondary break-all">
                镜像 {{ replicaDiff.differs.image ? '★' : '=' }} {{ row.image }}
              </div>
              <div class="text-small text-secondary">
                单卡余量 {{ replicaDiff.differs.headroomGb ? '★' : '=' }}
                {{ row.headroomGb !== null ? `${row.headroomGb}GB` : '--' }}
              </div>
            </div>
          </div>
        </template>

        <!-- 横向分割线 -->
        <div class="border-t border-line my-[1.2rem]" />

        <n-collapse>
          <n-collapse-item title="高级参数" name="advanced">
            <!-- 展开限高:仅高视口(≥40rem,媒体查询 rem 按初始字号 16px 计)限高内部滚动;小屏不限制,避免内外双滚动条 -->
            <div
              class="flex flex-col gap-[1.6rem] pt-[.4rem] [@media(max-height:100rem)]:max-h-[32rem] [@media(max-height:100rem)]:overflow-y-auto"
            >
              <!-- 启动参数:resolved.args 按配对口径成行回显(flag 与值合并一行),提交时拆回扁平 argv 作为 engine_params.extra_args -->
              <!-- 命中 preview_topology 引擎 protected_flags 前缀的行为系统必控项:只读且不可删;AutoComplete 聚焦出引擎 param_suggestions 推荐选项 -->
              <div class="flex flex-col gap-[.8rem]">
                <span class="text-small text-secondary">启动参数(args)</span>
                <div
                  v-for="index in editArgs.length"
                  :key="index"
                  class="flex items-center gap-[.8rem]"
                >
                  <n-auto-complete
                    v-model:value="editArgs[index - 1]"
                    :options="argOptions(editArgs[index - 1])"
                    :get-show="() => true"
                    :disabled="store.isArgProtected(editArgs[index - 1])"
                    class="font-mono min-w-0"
                    placeholder="如 --max-model-len 4096 或 --trust-remote-code"
                  />
                  <n-button
                    v-if="!store.isArgProtected(editArgs[index - 1])"
                    quaternary
                    type="error"
                    class="shrink-0"
                    :aria-label="`删除第 ${index} 条参数`"
                    @click="store.removeArg(index - 1)"
                  >
                    <template #icon>
                      <n-icon><CloseOutline /></n-icon>
                    </template>
                  </n-button>
                </div>
                <div v-if="!editArgs.length" class="text-base text-secondary">
                  暂无启动参数,可点击下方按钮添加
                </div>
                <div>
                  <n-button dashed size="small" @click="store.addArg()">
                    <template #icon>
                      <n-icon><AddOutline /></n-icon>
                    </template>
                    添加参数
                  </n-button>
                </div>
                <div class="text-base text-secondary">
                  每行一个参数,带值参数与值以空格分隔(如 --max-model-len 4096)
                </div>
                <div class="text-base text-secondary">
                  与系统必控项(如 --model、--tensor-parallel-size)冲突的参数将被忽略
                </div>
              </div>

              <!-- 环境变量:resolved.env 按 key-value 回显,提交时作为 engine_params.env -->
              <div class="flex flex-col gap-[.8rem]">
                <span class="text-small text-secondary">环境变量(env)</span>
                <div
                  v-for="(item, index) in editEnv"
                  :key="index"
                  class="flex items-center gap-[.8rem]"
                >
                  <n-input
                    v-model:value="item.key"
                    class="font-mono flex-[2] min-w-0"
                    placeholder="KEY"
                  />
                  <span class="text-secondary shrink-0">=</span>
                  <n-input
                    v-model:value="item.value"
                    class="font-mono flex-[3] min-w-0"
                    placeholder="value"
                  />
                  <n-button
                    quaternary
                    type="error"
                    size="small"
                    class="shrink-0"
                    :aria-label="`删除变量 ${item.key || index + 1}`"
                    @click="store.removeEnvVar(index)"
                  >
                    <template #icon>
                      <n-icon><CloseOutline /></n-icon>
                    </template>
                  </n-button>
                </div>
                <div v-if="!editEnv.length" class="text-small text-secondary">
                  暂无环境变量,可点击下方按钮添加
                </div>
                <div>
                  <n-button dashed size="small" @click="store.addEnvVar()">
                    <template #icon>
                      <n-icon><AddOutline /></n-icon>
                    </template>
                    添加变量
                  </n-button>
                </div>
              </div>
            </div>
          </n-collapse-item>
        </n-collapse>
      </div>
    </div>

    <!-- 预检告警:issues 非空但 deployable=true(纯告警,不阻塞创建) -->
    <LmAlert v-if="inspectResult?.issues.length" type="warning">
      <div v-for="issue in inspectResult.issues" :key="issue">
        {{ issue }}
      </div>
    </LmAlert>
    <LmAlert v-if="inspectResult?.cross_node" type="primary">
      当前配置将跨节点部署,多机 rendezvous 参数按实际调度节点定稿
    </LmAlert>

    <LmAlert type="warning"> 确认后将按所选 GPU 创建部署,创建过程可在列表查看进度 </LmAlert>
  </div>
</template>

<style scoped>
:deep(.n-collapse .n-collapse-item .n-collapse-item__header .n-collapse-item__header-main) {
  @apply text-base text-secondary;
}
</style>
