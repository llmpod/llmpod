<script setup lang="ts">
import { PlayCircleOutline, StopCircleOutline, TrashOutline } from '@vicons/ionicons5'
import { message, useConfirm } from '@repo/hooks'
import { toReplicaSeq, type DeploymentDetailOut } from '@/api/deployment'
import { deployStatusMap, formatDateTime, gpuSummary, useStore, type DeployRecord } from './useStore'
import { startDeploymentFlow } from './startFlow'

const props = defineProps<{ show: boolean; record: DeployRecord | null }>()
const emit = defineEmits<{ 'update:show': [value: boolean]; removed: [] }>()

const { fetchDetail, modelName, stop, remove } = useStore()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

/** 详情数据(打开抽屉时按 deployment_id 拉取;失败保留 null,各分区回退 --) */
const detail = shallowRef<DeploymentDetailOut | null>(null)
const detailLoading = ref(false)

async function loadDetail(deploymentId: string) {
  detailLoading.value = true
  try {
    detail.value = await fetchDetail(deploymentId)
  } catch {
    // 接口异常已由 HTTP 层统一提示,保留空态
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

watch(
  () => [props.show, props.record?.deployment_id] as const,
  ([show, deploymentId]) => {
    if (show && deploymentId) void loadDetail(deploymentId)
  },
)

/** 部署记录:详情接口返回后用最新数据(停止等操作后状态会推进),否则用列表行快照 */
const deployment = computed(() => detail.value?.deployment ?? props.record)

/** 状态标签配置 */
const statusConf = computed(() =>
  deployment.value
    ? (deployStatusMap[deployment.value.status] ?? {
        label: deployment.value.status,
        type: 'default' as const,
      })
    : deployStatusMap.stopped,
)

/** 可停止状态(后端 stop 允许 running/failed/creating) */
const stoppable = computed(
  () => !!deployment.value && ['running', 'failed', 'creating'].includes(deployment.value.status),
)

/** 可启动状态(后端 start 仅允许 stopped/failed) */
const startable = computed(
  () => !!deployment.value && ['failed', 'stopped'].includes(deployment.value.status),
)

/** 拓扑摘要 */
const topologyLabel = computed(() => {
  const topo = deployment.value?.spec?.topology
  if (!topo?.tp) return '--'
  return `tp=${topo.tp} · pp=${topo.pp ?? 1} · 副本 ${topo.replicas ?? 1} · 节点 ${
    topo.nnodes ?? 1
  }${topo.max_model_len ? ` · 上下文 ${topo.max_model_len}` : ''}`
})

/** 推理端点全集(多副本每份一条;后端恒给数组,空数组=未建端点) */
const endpoints = computed(() => {
  return detail.value?.endpoints ?? []
})

/** 端点 → 所属副本序号(replicas[].endpoint_ids 反查;旧数据无 id 映射为 null);返回 1 基展示序号(后端 seq 为 0 基) */
function endpointSeqOf(endpointId: number | undefined): number | null {
  if (endpointId == null) return null
  const replica = (detail.value?.replicas ?? []).find((item) =>
    item.endpoint_ids?.includes(endpointId),
  )
  return replica ? toReplicaSeq(replica.seq) : null
}

/** 健康态文案(healthy/degraded/down 中文映射;未知值原样) */
function healthLabel(status: string): string {
  return { healthy: '健康', degraded: '降级', down: '异常' }[status] ?? status
}

/** 副本分组视图行:replicas[].engines[].shard_ids 反查归属;未覆盖实例回退 null(平铺展示);seq 为 1 基展示序号 */
const instanceGroups = computed<{ seq: number | null; instances: DeploymentDetailOut['instances'] }[]>(() => {
  const data = detail.value
  if (!data) return []
  if (!data.replicas?.length) return [{ seq: null, instances: data.instances }]
  const groups: { seq: number | null; instances: DeploymentDetailOut['instances'] }[] =
    data.replicas.map((replica) => ({
      seq: toReplicaSeq(replica.seq),
      instances: data.instances.filter((instance) =>
        (replica.engines ?? []).some((engine) =>
          (engine.shard_ids ?? []).includes(instance.instance_id),
        ),
      ),
    }))
  // 副本层未覆盖的实例兜底平铺(数据不一致时仍可见)
  const covered = new Set(groups.flatMap((group) => group.instances.map((item) => item.instance_id)))
  const leftovers = data.instances.filter((item) => !covered.has(item.instance_id))
  if (leftovers.length) groups.push({ seq: null, instances: leftovers })
  return groups
})

/** 详情分区:基本信息 / 推理端点 / 资源信息 */
const sections = computed(() => {
  const data = deployment.value
  if (!data) return []
  return [
    {
      title: '基本信息',
      items: [
        { label: '部署名称', value: data.name },
        { label: '模型', value: modelName(data.model_id) },
        { label: '引擎', value: data.engine },
        { label: '状态', value: statusConf.value.label },
        { label: '创建时间', value: formatDateTime(data.created_at) },
        { label: '更新时间', value: formatDateTime(data.updated_at) },
      ],
    },
    {
      title: '推理端点',
      items: endpoints.value.length
        ? endpoints.value.flatMap((endpoint) => {
            const seq = endpointSeqOf(endpoint.id)
            const prefix = `副本${seq ?? '--'} · ${endpoint.name ?? ''}`
            return [
              { label: `${prefix} API 基址`, value: endpoint.api_base },
              { label: `${prefix} 请求路径`, value: endpoint.path ?? '--' },
              { label: `${prefix} served 模型`, value: endpoint.served_model },
              { label: `${prefix} 健康状态`, value: healthLabel(endpoint.health_status) },
            ]
          })
        : [{ label: '端点', value: '部署完成后生成' }],
    },
    {
      title: '资源信息',
      items: [
        { label: '算力(单副本)', value: gpuSummary(data) },
        { label: '并行拓扑', value: topologyLabel.value },
      ],
    },
  ]
})

/** 停止:确认后调接口,列表经 notify 刷新,抽屉内重拉详情同步状态 */
async function onStop() {
  const data = deployment.value
  if (!data) return
  try {
    await useConfirm({
      title: '停止部署',
      content: `确定停止部署「${data.name}」吗?停止后推理端点将不可用。`,
      confirmText: '停止',
      cancelText: '取消',
    })
  } catch {
    return
  }
  try {
    await stop(data.deployment_id)
    message.success(`已停止:${data.name}`)
    void loadDetail(data.deployment_id)
  } catch {
    // 接口异常已由 HTTP 层统一提示
  }
}

/** 启动:共享确认/强制启动流(列表与详情头部同一套),成功后重拉详情同步状态 */
async function onStart() {
  const data = deployment.value
  if (!data) return
  if (await startDeploymentFlow(data)) void loadDetail(data.deployment_id)
}

/** 删除:确认后调接口,成功后关闭抽屉并 emit removed(父级部署详情视图据此退出回列表),列表经 notify 刷新 */
async function onRemove() {
  const data = deployment.value
  if (!data) return
  try {
    await useConfirm({
      title: '删除部署',
      content: `确定删除部署「${data.name}」吗?将停止容器并删除实例与端点,不可恢复。`,
      confirmText: '删除',
      cancelText: '取消',
    })
  } catch {
    return
  }
  try {
    await remove(data.deployment_id)
    message.success(`已删除:${data.name}`)
    innerShow.value = false
    emit('removed')
  } catch {
    // 接口异常已由 HTTP 层统一提示
  }
}
</script>

<template>
  <n-drawer
    v-model:show="innerShow"
    :width="640"
  >
    <n-drawer-content closable>
      <template #header>
        <div class="flex items-center gap-[.8rem]">
          <span>部署详情-{{ deployment?.name ?? '' }}</span>
          <n-tag
            :type="statusConf.type"
            size="small"
            :bordered="false"
          >
            {{ statusConf.label }}
          </n-tag>
        </div>
      </template>

      <n-spin :show="detailLoading">
        <!-- 操作按钮:启动(可启状态)/停止(可停状态)/删除 -->
        <div class="flex items-center gap-[.8rem]">
          <n-button
            v-if="startable"
            size="small"
            type="primary"
            ghost
            @click="onStart"
          >
            <template #icon>
              <n-icon :component="PlayCircleOutline" />
            </template>
            启动
          </n-button>
          <n-button
            v-if="stoppable"
            size="small"
            @click="onStop"
          >
            <template #icon>
              <n-icon :component="StopCircleOutline" />
            </template>
            停止
          </n-button>
          <n-button
            size="small"
            type="error"
            ghost
            @click="onRemove"
          >
            <template #icon>
              <n-icon :component="TrashOutline" />
            </template>
            删除
          </n-button>
        </div>

        <!-- 失败原因:部署失败时置顶展示 -->
        <LmAlert
          v-if="deployment?.last_error"
          type="error"
          class="mt-[1.2rem]"
        >
          {{ deployment.last_error }}
        </LmAlert>

        <!-- 详情分区 -->
        <div
          v-for="section in sections"
          :key="section.title"
        >
          <div class="detail-section-title">
            {{ section.title }}
          </div>
          <n-descriptions
            size="small"
            :column="1"
            bordered
            label-placement="left"
            label-class="w-[16rem]"
          >
            <n-descriptions-item
              v-for="item in section.items"
              :key="item.label"
              :label="item.label"
            >
              {{ item.value }}
            </n-descriptions-item>
          </n-descriptions>
        </div>

        <!-- 实例列表:按副本分组(标题 rN);副本层缺失时平铺不分组的原形态 -->
        <div v-if="detail?.instances.length">
          <div class="detail-section-title">
            实例({{ detail.instances.length }})
          </div>
          <div
            v-for="group in instanceGroups"
            :key="group.seq ?? 'plain'"
            class="flex flex-col gap-[.8rem]"
          >
            <div v-if="group.seq != null" class="text-small text-secondary mt-[.4rem]">
              副本{{ group.seq }}
            </div>
            <div
              v-for="instance in group.instances"
              :key="instance.instance_id"
              class="rounded-[.8rem] border border-line bg-fill-1 px-[1.2rem] py-[.8rem] text-small"
            >
              <div class="flex items-center gap-[.8rem]">
                <span class="text-strong font-medium">#{{ instance.rank }} {{ instance.role }}</span>
                <n-tag
                  size="tiny"
                  :bordered="false"
                >
                  {{ instance.status }}
                </n-tag>
              </div>
              <div class="text-secondary mt-[.4rem]">
                节点 {{ instance.assigned_node ?? '待调度' }} · 端点 {{ instance.endpoint ?? '--' }}
              </div>
              <div
                v-if="instance.last_error"
                class="text-error mt-[.4rem]"
              >
                {{ instance.last_error }}
              </div>
            </div>
          </div>
        </div>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.detail-section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--c-strong);
  margin: 20px 0 10px;
}
</style>
