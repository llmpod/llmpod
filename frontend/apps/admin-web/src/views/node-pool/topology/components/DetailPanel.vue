<script setup lang="ts">
import { CloseOutline } from '@vicons/ionicons5'
import type { GraphNodeType, PanelSelection } from '../graph'
import {
  DEPLOY_HEALTH_MAP,
  DEPLOY_STATUS_MAP,
  GPU_STATUS_MAP,
  NODE_STATUS_MAP,
  type ClusterNodeData,
  type DeployNodeData,
  type EngineNodeData,
  type FlowModel,
  type GpuNodeData,
  type NodeNodeData,
} from '../useStore'

const props = defineProps<{ selection: PanelSelection | null }>()
const emit = defineEmits<{ close: []; jump: [] }>()

const TYPE_TITLE: Partial<Record<GraphNodeType, string>> = {
  cluster: '集群详情',
  node: '节点详情',
  gpu: 'GPU 详情',
  deploy: '部署详情',
  engine: '引擎详情',
  model: '模型详情',
}

const title = computed(() => (props.selection ? (TYPE_TITLE[props.selection.type] ?? '详情') : ''))

/** 聚合块无对应管理页,不显示跳转按钮 */
const canJump = computed(() => !!props.selection && props.selection.type !== 'aggregate')

const rows = computed<[string, string][]>(() => {
  const sel = props.selection
  if (!sel) return []
  switch (sel.type) {
    case 'cluster': {
      const cluster = sel.record as ClusterNodeData
      return [
        ['名称', cluster.name],
        ['类型', cluster.type === 'k8s' ? 'Kubernetes' : 'Docker'],
        ['状态', cluster.status === 'active' ? '启用' : '停用'],
        ['节点数', String(cluster.nodeCount)],
        ['GPU 总数', String(cluster.gpuTotal)],
      ]
    }
    case 'node': {
      const nodeItem = sel.record as NodeNodeData
      const first = nodeItem.gpus[0]
      // 离线节点运行时字段后端置 0,展示 -- 以区分「离线」与「在线空闲(0%)」
      const online = nodeItem.status === 'online'
      return [
        ['主机名', nodeItem.hostname],
        ['IP', nodeItem.ip],
        ['状态', NODE_STATUS_MAP[nodeItem.status]?.label ?? nodeItem.status],
        ['CPU 使用率', online ? `${Math.round(nodeItem.cpuPercent)}%` : '--'],
        [
          '内存',
          online
            ? `${nodeItem.memoryUsedGb}/${nodeItem.memoryGb}GB（${Math.round(nodeItem.memoryPercent)}%）`
            : `--/${nodeItem.memoryGb}GB`,
        ],
        ['GPU', first ? `${first.name} × ${nodeItem.gpus.length}` : '--'],
        ['部署数', `${nodeItem.deployCount} 个`],
        ['GPU 列表', nodeItem.gpus.length ? nodeItem.gpus.map((gpu) => `${gpu.name}#${gpu.id}`).join('、') : '--'],
        ['部署列表', nodeItem.deployNames.length ? nodeItem.deployNames.join('、') : '无'],
        ['承载副本', nodeItem.replicaRefs.length ? nodeItem.replicaRefs.join('、') : '无'],
      ]
    }
    case 'gpu': {
      const gpu = sel.record as GpuNodeData
      return [
        ['型号', gpu.name],
        ['编号', `#${gpu.id}`],
        ['所属节点', gpu.nodeHost],
        ['显存', `已用 ${gpu.usedVramGb}/${gpu.memory}GB`],
        ['可用显存', gpu.availVramGb != null ? `${gpu.availVramGb}GB` : '--'],
        ['利用率', `${gpu.util}%`],
        ['状态', GPU_STATUS_MAP[gpu.status]?.label ?? gpu.status],
      ]
    }
    case 'deploy': {
      const deploy = sel.record as DeployNodeData
      return [
        ['名称', deploy.name],
        ['状态', DEPLOY_STATUS_MAP[deploy.status]?.label ?? deploy.status],
        ['模型', deploy.modelName],
        ['引擎', deploy.engineName],
        ['副本', String(deploy.replicas)],
        // 健康态:集群拓扑实时探测,附在副本行(healthy 绿/degraded 黄/down 红;无数据为 --)
        ['健康状态', deploy.healthStatus ? (DEPLOY_HEALTH_MAP[deploy.healthStatus]?.label ?? deploy.healthStatus) : '--'],
        ['占用 GPU', `× ${deploy.gpuUuids.length}`],
        ['访问地址', deploy.endpoint ?? '--'],
      ]
    }
    case 'engine': {
      const engine = sel.record as EngineNodeData
      return [
        ['名称', engine.name],
        ['版本', engine.version],
        ['关联部署', `${engine.deployCount} 个`],
      ]
    }
    case 'model': {
      const model = sel.record as FlowModel
      return [
        ['名称', model.name],
        ['分类', model.category],
        ['格式', model.format ?? '--'],
        ['大小', model.sizeGb === null ? '--' : `${model.sizeGb}GB`],
      ]
    }
    default:
      return []
  }
})
</script>

<template>
  <transition name="panel-slide">
    <div
      v-if="selection"
      class="absolute top-[1.6rem] right-[1.6rem] z-10 w-[28rem] max-w-[calc(100%-1.6rem)] max-h-[calc(100%-3.2rem)] overflow-auto rounded-lg border border-line-strong bg-fill-1 shadow-lg p-[1.6rem]"
    >
      <div class="flex items-center justify-between mb-[1.2rem]">
        <span class="text-base text-strong font-medium">{{ title }}</span>
        <n-icon :size="18" class="cursor-pointer text-icon" @click="emit('close')">
          <CloseOutline />
        </n-icon>
      </div>
      <div class="flex flex-col gap-[.8rem]">
        <div v-for="row in rows" :key="row[0]" class="flex justify-between text-small">
          <span class="text-secondary shrink-0">{{ row[0] }}</span>
          <span class="text-default text-right truncate pl-[1.2rem]">{{ row[1] }}</span>
        </div>
      </div>
      <n-button v-if="canJump" type="primary" size="small" block class="mt-[1.6rem]" @click="emit('jump')">
        前往管理页
      </n-button>
    </div>
  </transition>
</template>

<style lang="scss" scoped>
.panel-slide-enter-active,
.panel-slide-leave-active {
  transition: all 0.2s ease;
}
.panel-slide-enter-from,
.panel-slide-leave-to {
  opacity: 0;
  transform: translateX(1.6rem);
}
</style>
