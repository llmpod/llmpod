<script setup lang="ts">
import { message, useConfirm } from '@repo/hooks'
import {
  useStore,
  nodeStatusMap,
  nodeVersionStatusMap,
  formatDateTime,
  formatPercent,
  formatGbPair,
  type ComputeNode,
  type NodeData,
} from './useStore'

const props = defineProps<{
  show: boolean
  node: ComputeNode | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const { getDetail, toggleMaintenance, remove } = useStore()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const detail = ref<NodeData>()
const loading = ref(false)
/** 操作进行中(维护模式切换/注销),防止重复点击 */
const operating = ref(false)

watch(
  () => props.show,
  async (visible) => {
    if (!visible || !props.node) return
    loading.value = true
    detail.value = await getDetail(props.node.id)
    loading.value = false
  },
)

/** CPU 文案:核数 + 架构;空值显示 — */
const cpuText = computed(() => {
  if (!detail.value) return '—'
  const { cpu_cores, cpu_arch } = detail.value
  if (cpu_cores == null && !cpu_arch) return '—'
  return [cpu_cores != null ? `${cpu_cores} 核` : '', cpu_arch ?? '']
    .filter(Boolean)
    .join(' · ')
})

/** 实时资源使用(runtime 来自心跳缓冲;离线节点为 null,各字段渲染 —,不当 0 用) */
const runtime = computed(() => detail.value?.runtime ?? null)

/** 内存用量文案:已用/总量(使用率);总量取节点静态字段 memory_gb */
const memoryUsageText = computed(() => {
  const rt = runtime.value
  if (!rt || !detail.value) return '—'
  return `${formatGbPair(rt.memory_used_gb, detail.value.memory_gb)}（${formatPercent(rt.memory_percent)}）`
})

/** 磁盘用量文案:已用/总量(使用率) */
const diskUsageText = computed(() => {
  const rt = runtime.value
  if (!rt) return '—'
  return `${formatGbPair(rt.disk_used_gb, rt.disk_total_gb)}（${formatPercent(rt.disk_percent)}）`
})

/** 显存用量文案:已用/总量(逐卡求和);无 GPU 显示 — */
const vramUsageText = computed(() => {
  const rt = runtime.value
  if (!rt || !rt.gpu_count) return '—'
  return formatGbPair(rt.used_vram_gb, rt.total_vram_gb)
})

/** 维护模式切换:online ↔ maintenance,二次确认后执行,完成后刷新详情与列表 */
async function onToggleMaintenance() {
  if (!detail.value) return
  const entering = detail.value.status !== 'maintenance'
  try {
    await useConfirm({
      title: entering ? '进入维护模式' : '退出维护模式',
      content: entering
        ? '确定将节点「' + detail.value.hostname + '」切换为维护模式吗?维护期间节点将停止调度新任务。'
        : '确定将节点「' + detail.value.hostname + '」退出维护模式吗?退出后节点恢复参与调度。',
      confirmText: entering ? '进入维护' : '退出维护',
      cancelText: '取消',
    })
  }
  catch {
    return
  }
  operating.value = true
  try {
    const next = await toggleMaintenance(detail.value)
    detail.value = await getDetail(detail.value.id)
    message.success(next === 'maintenance' ? '已进入维护模式' : '已退出维护模式')
  }
  finally {
    operating.value = false
  }
}

/** 注销节点:二次确认(在线节点会被后端拒绝),成功后关闭抽屉并刷新列表 */
async function onRemove() {
  if (!detail.value) return
  try {
    await useConfirm({
      title: '注销节点',
      content: `确定注销节点「${detail.value.hostname}」吗?节点记录及其 GPU 记录将被删除,不可恢复。`,
      confirmText: '注销',
      cancelText: '取消',
    })
  }
  catch {
    return
  }
  operating.value = true
  try {
    await remove(detail.value.id)
    message.success(`已注销:${detail.value.hostname}`)
    innerShow.value = false
  }
  finally {
    operating.value = false
  }
}
</script>

<template>
  <n-drawer
    v-model:show="innerShow"
    :width="680"
  >
    <n-drawer-content
      :title="`${node?.hostname ?? ''} · 节点详情`"
      closable
    >
      <n-spin :show="loading">
        <div
          v-if="detail"
          class="flex flex-col gap-[1rem]"
        >
          <!-- 基本信息 -->
          <section>
            <div class="drawer-subtitle">
              基本信息
            </div>
            <n-descriptions
              size="small"
              :column="1"
              bordered
              label-placement="left"
              label-class="w-[16rem]"
            >
              <n-descriptions-item label="主机名">
                {{ detail.hostname }}
              </n-descriptions-item>
              <n-descriptions-item label="IP">
                {{ detail.ip }}
              </n-descriptions-item>
              <n-descriptions-item label="所属集群">
                {{ detail.cluster_id }}
              </n-descriptions-item>
              <n-descriptions-item label="状态">
                <n-tag
                  size="small"
                  :type="(nodeStatusMap[detail.status] ?? { label: detail.status, type: 'default' }).type"
                  :bordered="false"
                >
                  {{ (nodeStatusMap[detail.status] ?? { label: detail.status, type: 'default' }).label }}
                </n-tag>
              </n-descriptions-item>
              <n-descriptions-item label="版本">
                <span v-if="detail.version">
                  <span class="tabular-nums">{{ detail.version }}</span>
                  <n-tag
                    v-if="detail.version_status !== 'ok'"
                    size="small"
                    :bordered="false"
                    :type="nodeVersionStatusMap[detail.version_status]?.type ?? 'default'"
                    class="ml-[.4rem]"
                  >
                    {{ nodeVersionStatusMap[detail.version_status]?.label ?? detail.version_status }}
                  </n-tag>
                </span>
                <span
                  v-else
                  class="text-secondary"
                >—</span>
              </n-descriptions-item>
              <!-- 备注:暂无此功能,先隐藏
              <n-descriptions-item label="备注">
                {{ detail.remark ?? '—' }}
              </n-descriptions-item>
              -->
              <n-descriptions-item label="CPU">
                {{ cpuText }}
              </n-descriptions-item>
              <n-descriptions-item label="内存">
                {{ detail.memory_gb != null ? `${detail.memory_gb} GB` : '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="最近心跳">
                {{ formatDateTime(detail.last_heartbeat_at) }}
              </n-descriptions-item>
              <n-descriptions-item label="创建时间">
                {{ formatDateTime(detail.created_at) }}
              </n-descriptions-item>
            </n-descriptions>
          </section>

          <!-- 资源使用(实时,离线节点 runtime 为 null) -->
          <section>
            <div class="drawer-subtitle">
              资源使用
            </div>
            <n-descriptions
              size="small"
              :column="1"
              bordered
              label-placement="left"
              label-class="w-[16rem]"
            >
              <n-descriptions-item label="CPU 使用率">
                {{ runtime ? formatPercent(runtime.cpu_percent) : '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="内存">
                {{ memoryUsageText }}
              </n-descriptions-item>
              <n-descriptions-item label="磁盘(根分区)">
                {{ diskUsageText }}
              </n-descriptions-item>
              <n-descriptions-item label="GPU 数量">
                {{ runtime ? runtime.gpu_count : '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="GPU 利用率">
                {{ runtime && runtime.gpu_count ? formatPercent(runtime.gpu_utilization) : '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="显存">
                {{ vramUsageText }}
              </n-descriptions-item>
            </n-descriptions>
          </section>

          <!-- 节点操作 -->
          <section>
            <div class="drawer-subtitle">
              节点操作
            </div>
            <div class="flex gap-[.5rem] border p-[1.6rem] rounded">
              <n-button
                tertiary
                :loading="operating"
                @click="onToggleMaintenance"
              >
                {{ detail.status === 'maintenance' ? '退出维护' : '维护模式' }}
              </n-button>
              <n-button
                tertiary
                type="error"
                :loading="operating"
                @click="onRemove"
              >
                注销节点
              </n-button>
            </div>
          </section>
        </div>
        <div
          v-else
          class="min-h-[320px]"
        />
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped lang="scss">
.drawer-subtitle {
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
