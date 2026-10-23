<script setup lang="ts">
import { CloseOutline, TerminalOutline } from '@vicons/ionicons5'
import {
  GPU_STATUS_MAP,
  NODE_STATUS_MAP,
} from '@/views/node-pool/topology/useStore'
import type {
  DeploymentNodeData,
  GpuNodeData,
  InstanceNodeData,
  PanelSelection,
  PhyNodeData,
  ReplicaNodeData,
} from '../graph'
import { HEALTH_STATUS_TEXT } from '../graph'
import type { NodeAssetsView } from '../useStore'

const props = defineProps<{
  selection: PanelSelection | null
  /** 物理节点资产(主机名/IP/GPU);点击 phy 节点后异步回填 */
  nodeAssets: NodeAssetsView | null
  assetsLoading: boolean
}>()
/** 实例详情底部「启动命令」入口(弹窗展示该容器完整 docker run);面板纯展示,动作上抛 */
const emit = defineEmits<{ close: []; viewCommand: [instanceId: string] }>()

/** 实例选中态(record 收窄;非实例为 null) */
const instanceSel = computed(() =>
  props.selection?.type === 'instance'
    ? (props.selection.record as InstanceNodeData)
    : null,
)

const TYPE_TITLE: Record<string, string> = {
  deployment: '部署详情',
  replica: '副本详情',
  instance: '实例详情',
  phy: '节点详情',
  gpu: 'GPU 详情',
}

const title = computed(() =>
  props.selection ? (TYPE_TITLE[props.selection.type] ?? '详情') : '',
)

/** 资产未就绪时的占位文案(加载中/拉取失败均为 --,由 loading 态区分) */
function assetText(value: string | undefined | null): string {
  if (value != null && value !== '') return value
  return props.assetsLoading ? '加载中…' : '--'
}

/** 状态点色类 → 文本色类(bg-success → text-success),状态行文本按同套语义色上色 */
function dotToText(dot?: string | null): string | undefined {
  return dot ? dot.replace(/^bg-/, 'text-') : undefined
}

/** TagType → 文本色类(徽标步骤态) */
const TAG_TEXT: Record<string, string> = {
  info: 'text-info',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-error',
  default: 'text-secondary',
}

/** 行结构:[标签, 值, 可选文本色类(状态/错误行), 可选完整展示(不省略,长文本换行)] */
const rows = computed<[string, string, string?, boolean?][]>(() => {
  const sel = props.selection
  if (!sel) return []
  switch (sel.type) {
    case 'deployment': {
      const deploy = sel.record as DeploymentNodeData
      const topo = deploy.topology
      return [
        ['名称', deploy.name],
        [
          '状态',
          deploy.phaseLabel
            ? `${deploy.statusLabel} · ${deploy.phaseLabel}`
            : deploy.statusLabel,
          dotToText(deploy.statusDot),
        ],
        ['模型', deploy.modelName],
        ['引擎', deploy.engine],
        ['TP 并行', topo ? `× ${topo.tp}` : '--'],
        ['PP 流水', topo ? `× ${topo.pp}` : '--'],
        ['副本数', topo ? String(topo.replicas) : '--'],
        // 健康态:集群拓扑实时探测(healthy 绿/degraded 黄/down 红;无数据为 --)
        [
          '健康状态',
          deploy.healthStatus
            ? (HEALTH_STATUS_TEXT[deploy.healthStatus]?.label ?? deploy.healthStatus)
            : '--',
          deploy.healthStatus ? HEALTH_STATUS_TEXT[deploy.healthStatus]?.text : undefined,
        ],
        ['节点数', topo ? String(topo.nnodes) : '--'],
        ['上下文长度', topo?.maxModelLen ? String(topo.maxModelLen) : '--'],
        ['创建时间', deploy.createdAt],
        ['更新时间', deploy.updatedAt],
      ]
    }
    case 'replica': {
      const replica = sel.record as ReplicaNodeData
      // 引擎明细逐行(份内多引擎时;v1 每份单引擎即一行)
      const engineRows: [string, string, string?, boolean?][] = replica.engines.map(
        (engine, index) => [
          `引擎 #${index + 1}`,
          [engine.role, engine.parallel, engine.gpuArch, engine.status]
            .filter(Boolean)
            .join(' · '),
        ],
      )
      return [
        ['副本', `副本${replica.seq}${replica.replicaId ? '' : '(推导)'}`],
        ['状态', replica.statusLabel, dotToText(replica.statusDot)],
        ...engineRows,
        ['分片数', String(replica.shardCount)],
        ['端点数', replica.endpointCount ? `${replica.endpointCount} 条` : '--'],
        ['副本 ID', replica.replicaId ?? '--'],
        // replicas 层缺失时该份由分片分布推导,标注来源提示口径
        ...(replica.derived
          ? ([['数据来源', '副本层缺失,按分片分布推导']] as [string, string, string?, boolean?][])
          : []),
      ]
    }
    case 'instance': {
      const inst = sel.record as InstanceNodeData
      return [
        ['实例', `#${inst.rank} ${inst.role}`],
        ['角色', inst.head ? 'Head' : 'Worker'],
        ['副本', inst.replicaCount > 1 ? `副本${inst.replicaSeq} / 共 ${inst.replicaCount}` : '单副本'],
        ['状态', inst.statusLabel, dotToText(inst.statusDot)],
        ['所在节点', inst.nodeId ?? '待调度'],
        ['容器 ID', inst.containerId ?? '--'],
        ['服务端点', inst.endpoint ?? '--'],
        ['启动时间', inst.startedAt],
        ['就绪时间', inst.readyAt],
        ['停止时间', inst.stoppedAt],
        // 有错误:红字完整换行展示(不省略);无错误:常规单行「无」
        [
          '最近错误',
          inst.lastError ?? '无',
          inst.lastError ? 'text-error' : undefined,
          Boolean(inst.lastError),
        ],
      ]
    }
    case 'phy': {
      const phy = sel.record as PhyNodeData
      const asset = props.nodeAssets?.nodes.get(phy.nodeId)
      const gpus = props.nodeAssets?.gpus.get(phy.nodeId) ?? []
      const nodeConf = asset ? NODE_STATUS_MAP[asset.status] : undefined
      const list: [string, string, string?, boolean?][] = [
        ['节点 ID', phy.nodeId],
        ['主机名', assetText(asset?.hostname)],
        ['IP', assetText(asset?.ip)],
        [
          '节点状态',
          assetText(nodeConf?.label ?? asset?.status ?? null),
          dotToText(nodeConf?.dot),
        ],
        // 离线节点 CPU/内存后端置 0,展示 -- 以区分「离线」与「在线空闲(0%)」
        [
          'CPU 使用率',
          asset
            ? asset.status === 'online'
              ? `${Math.round(asset.cpu_percent)}%`
              : '--'
            : assetText(null),
        ],
        [
          '内存',
          asset
            ? asset.status === 'online'
              ? `${asset.memory_used_gb}/${asset.memory_gb}GB（${Math.round(asset.memory_percent)}%）`
              : `--/${asset.memory_gb}GB`
            : assetText(null),
        ],
        ['GPU 数', props.nodeAssets ? String(gpus.length) : assetText(null)],
      ]
      // GPU 明细:每卡一行(型号 · 显存/已用/可用 · 利用率 · 状态)
      gpus.forEach((gpu) => {
        list.push([
          `GPU #${gpu.index}`,
          [
            gpu.name,
            `${gpu.total_vram_gb}GB / 已用 ${gpu.used_vram_gb}GB${gpu.available_vram_gb != null ? ` / 可用 ${gpu.available_vram_gb}GB` : ''}`,
            gpu.util != null ? `利用率 ${gpu.util}%` : '利用率 --',
            GPU_STATUS_MAP[gpu.status]?.label ?? gpu.status,
          ].join(' · '),
        ])
      })
      list.push([
        '承载实例',
        phy.instances.length
          ? phy.instances.map((item) => `${item.label}(${item.statusLabel})`).join('、')
          : '--',
      ])
      if (phy.badge) {
        list.push(
          ['当前步骤', `${phy.badge.label} · ${phy.badge.statusLabel}`, TAG_TEXT[phy.badge.type]],
          ['最后消息', phy.badge.message ?? '--', phy.badge.type === 'error' ? 'text-error' : undefined],
          ['更新时间', phy.badge.updatedAt],
        )
      }
      return list
    }
    case 'gpu': {
      const gpu = sel.record as GpuNodeData
      const nodeAsset = props.nodeAssets?.nodes.get(gpu.node_id)
      const gpuConf = GPU_STATUS_MAP[gpu.status]
      return [
        ['型号', gpu.name],
        ['编号', `#${gpu.index}`],
        ['UUID', gpu.uuid],
        ['所在节点', nodeAsset?.hostname ?? gpu.node_id],
        ['状态', gpuConf?.label ?? gpu.status, dotToText(gpuConf?.dot)],
        [
          '显存',
          `${gpu.used_vram_gb}/${gpu.total_vram_gb}GB${
            gpu.available_vram_gb != null ? `（可用 ${gpu.available_vram_gb}GB）` : ''
          }`,
        ],
        ['利用率', gpu.util != null ? `${gpu.util}%` : '--'],
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
          <!-- 状态/错误行带语义色(row[2]);wrap 行(row[3],如最近错误)完整换行不省略,其余溢出省略 + 悬浮 -->
          <span
            class="text-right pl-[1.2rem]"
            :class="[row[2] ?? 'text-default', row[3] ? 'break-words' : 'truncate']"
            :title="row[3] ? undefined : row[1]"
          >
            {{ row[1] }}
          </span>
        </div>
      </div>
      <!-- 实例详情底部:启动命令入口(低频诊断动作,藏于面板不占卡片) -->
      <div v-if="instanceSel" class="mt-[.4rem] flex justify-end border-t border-line pt-[.8rem]">
        <n-button
          text
          type="primary"
          size="tiny"
          @click="emit('viewCommand', instanceSel.instanceId)"
        >
          <template #icon>
            <n-icon :component="TerminalOutline" />
          </template>
          启动命令
        </n-button>
      </div>
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
