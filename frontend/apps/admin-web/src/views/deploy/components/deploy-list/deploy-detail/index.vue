<script setup lang="ts">
import {
  ArrowBackOutline,
  CheckmarkOutline,
  CopyOutline,
  DocumentTextOutline,
  DuplicateOutline,
  HourglassOutline,
  InformationCircleOutline,
  PlayCircleOutline,
  StopCircleOutline,
  TrashOutline,
} from '@vicons/ionicons5'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import type { DeployRecord } from '../useStore'
import DeployDetailDrawer from '../DeployDetailDrawer.vue'
import DeployPhaseSteps from '../DeployPhaseSteps.vue'
import DeploymentNode from './components/DeploymentNode.vue'
import DetailPanel from './components/DetailPanel.vue'
import GpuNode from './components/GpuNode.vue'
import InstanceNode from './components/InstanceNode.vue'
import PhyNode from './components/PhyNode.vue'
import ReplicaNode from './components/ReplicaNode.vue'
import { HEALTH_STATUS_TEXT } from './graph'
import { useController } from './useController'

const props = defineProps<{ record: DeployRecord }>()
const emit = defineEmits<{ exit: [] }>()

const {
  deployment,
  statusConf,
  phaseLabel,
  phaseCurrent,
  stoppable,
  startable,
  deletable,
  endpoints,
  endpoint,
  endpointOptions,
  endpointUrl,
  activeEndpointIndex,
  selectEndpoint,
  scalable,
  scaling,
  openScaleDialog,
  copiedKey,
  copyText,
  detailDrawerShow,
  openDetailDrawer,
  nodes,
  edges,
  initialLoading,
  panelSelection,
  closePanel,
  openLogDialog,
  openCommandDialog,
  nodeAssets,
  assetsLoading,
  onStop,
  onStart,
  onRemove,
  back,
} = useController(props.record, () => emit('exit'))

/** 当前端点健康徽标(healthy/degraded/down → 文案 + 语义色) */
const endpointHealth = computed(() =>
  endpoint.value ? (HEALTH_STATUS_TEXT[endpoint.value.health_status] ?? null) : null,
)
</script>

<template>
  <div>
    <!-- 头部:返回 + 名称/状态 + 生命周期操作 -->
    <div class="flex items-center justify-between gap-[1.2rem] flex-wrap">
      <div class="flex items-center gap-[.8rem] flex-wrap">
        <n-button quaternary @click="back">
          <template #icon>
            <n-icon :component="ArrowBackOutline" />
          </template>
          返回
        </n-button>
        <span class="text-[1.6rem] font-semibold text-strong">
          部署详情-{{ deployment.name }}
        </span>
        <n-tag :type="statusConf.type" size="small" :bordered="false">
          {{ statusConf.label }}
        </n-tag>
        <!-- 部署中:附编排子态进度(与列表状态列口径一致);在线扩缩容期间附 SCALING 子态 -->
        <span
          v-if="deployment.status === 'creating' || deployment.phase === 'SCALING'"
          class="text-base text-tertiary"
        >
          {{ phaseLabel }}
        </span>
        <!-- 详情抽屉入口:基本信息/推理端点/实例等分区 -->
        <n-button size="small" quaternary @click="openDetailDrawer">
          <template #icon>
            <n-icon :component="InformationCircleOutline" />
          </template>
          详情
        </n-button>
      </div>
      <div class="flex items-center gap-[.8rem]">
        <n-button v-if="startable" size="small" type="primary" ghost @click="onStart">
          <template #icon>
            <n-icon :component="PlayCircleOutline" />
          </template>
          启动
        </n-button>
        <!-- 在线扩缩容:仅 running 可用;SCALING 子态禁用防重入(tooltip 需随按钮整体隐藏,
             空 trigger 槽会让浮层定位组件报错) -->
        <n-tooltip v-if="scalable" :disabled="!scaling" trigger="hover">
          <template #trigger>
            <n-button
              size="small"
              type="primary"
              ghost
              :disabled="scaling"
              @click="openScaleDialog"
            >
              <template #icon>
                <n-icon :component="DuplicateOutline" />
              </template>
              {{ scaling ? '扩缩容中…' : '调整副本' }}
            </n-button>
          </template>
          扩缩容进行中,请等待完成后再调整
        </n-tooltip>
        <n-button v-if="stoppable" size="small" @click="onStop">
          <template #icon>
            <n-icon :component="StopCircleOutline" />
          </template>
          停止
        </n-button>
        <n-button v-if="deletable" size="small" type="error" ghost @click="onRemove">
          <template #icon>
            <n-icon :component="TrashOutline" />
          </template>
          删除
        </n-button>
      </div>
    </div>

    <!-- 失败原因:部署失败时置顶展示 -->
    <LmAlert v-if="deployment.last_error" type="error" class="mt-[1.2rem]">
      {{ deployment.last_error }}
    </LmAlert>

    <!-- 推理端点(running 后非空);多副本时每份一条,下拉按「rN · 健康态」切换 -->
    <div
      v-if="endpoint"
      class="mt-[1.2rem] flex items-center gap-[1.6rem] flex-wrap rounded-[.8rem] border border-line bg-fill-1 px-[1.2rem] py-[.8rem] text-small"
    >
      <span class="text-secondary shrink-0">推理端点</span>
      <!-- 多端点切换:单端点不渲染下拉(保持原形态);label 为「rN · 健康态」 -->
      <LmSelect
        v-if="endpoints.length > 1"
        :value="activeEndpointIndex"
        :options="endpointOptions"
        size="small"
        class="w-[14rem]"
        @update:value="selectEndpoint"
      />
      <span class="flex items-center gap-[.4rem]">
        <span class="font-mono text-default">{{ endpointUrl }}</span>
        <n-button
          text
          size="tiny"
          title="复制端点"
          @click="copyText(endpointUrl, 'endpoint')"
        >
          <template #icon>
            <n-icon :component="copiedKey === 'endpoint' ? CheckmarkOutline : CopyOutline" />
          </template>
        </n-button>
      </span>
      <span class="flex items-center gap-[.4rem] text-secondary">
        served 模型 <span class="text-default">{{ endpoint.served_model }}</span>
        <n-button
          text
          size="tiny"
          title="复制 served 模型"
          @click="copyText(endpoint.served_model, 'servedModel')"
        >
          <template #icon>
            <n-icon :component="copiedKey === 'servedModel' ? CheckmarkOutline : CopyOutline" />
          </template>
        </n-button>
      </span>
      <!-- 端点健康徽标:集群拓扑实时探测(healthy 绿/degraded 黄/down 红) -->
      <span
        v-if="endpointHealth"
        class="flex items-center gap-[.4rem]"
        :class="endpointHealth.text"
      >
        <span class="w-[.8rem] h-[.8rem] rounded-full" :class="endpointHealth.dot" />
        {{ endpointHealth.label }}
      </span>
    </div>

    <!-- 拓扑画布:部署 → 物理节点 → head → workers → GPU(该部署占用,数据源集群拓扑),
         与 node-pool 拓扑视图同一套画布与样式;日志入口在节点详情面板(弹窗打开) -->
    <div
      class="relative mt-[1.6rem] h-[calc(100vh-33rem)] min-h-[42rem] rounded-lg border border-line overflow-hidden"
    >
      <VueFlow
        :nodes="nodes"
        :edges="edges"
        class="workflow-flow bg-fill-2"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :min-zoom="0.2"
        :max-zoom="1.5"
      >
        <Background :gap="24" />
        <Controls position="bottom-left" />
        <MiniMap pannable zoomable />
        <!-- 各层节点卡片自带「日志」入口(viewLogs 携带对应 entry → 日志弹窗) -->
        <template #node-deployment="nodeProps">
          <DeploymentNode v-bind="nodeProps" @view-logs="openLogDialog" />
        </template>
        <template #node-replica="nodeProps">
          <ReplicaNode v-bind="nodeProps" />
        </template>
        <template #node-instance="nodeProps">
          <InstanceNode v-bind="nodeProps" @view-logs="openLogDialog" />
        </template>
        <template #node-phy="nodeProps">
          <PhyNode v-bind="nodeProps" @view-logs="openLogDialog" />
        </template>
        <template #node-gpu="nodeProps">
          <GpuNode v-bind="nodeProps" />
        </template>
      </VueFlow>
      <!-- 编排进度:creating 期间固定在画布顶部并水平居中(不随画布平移/缩放;纯展示,层级低于详情面板);
           宽度不足(窄视口)时步骤条横向滚动兜底,不挤压变形 -->
      <div
        v-if="deployment.status === 'creating'"
        class="absolute top-[1.6rem] inset-x-0 z-[5] mx-auto w-[111rem] max-w-[calc(100%-3.2rem)] rounded-lg border border-line-strong bg-fill-1 px-[1.2rem] py-[.8rem] shadow-sm"
      >
        <div class="flex items-center gap-[.6rem]">
          <n-icon :size="18" class="text-primary shrink-0"><HourglassOutline /></n-icon>
          <span class="text-small text-strong font-medium">编排进度</span>
          <span class="text-xs text-secondary">{{ phaseLabel }}</span>
          <!-- 编排日志入口:与部署节点卡片「编排日志」同一弹窗(全部署时间线) -->
          <n-button
            text
            type="primary"
            size="tiny"
            class="ml-auto mr-[.4rem]"
            @click="openLogDialog({ type: 'timeline' })"
          >
            <template #icon>
              <n-icon :component="DocumentTextOutline" />
            </template>
            查看日志
          </n-button>
        </div>
        <div class="mt-[.6rem] overflow-x-auto">
          <DeployPhaseSteps :current="phaseCurrent" class="min-w-[100rem]" />
        </div>
      </div>
      <!-- 节点详情面板:点击节点在画布右上角展示(拓扑属性/GPU 节点属性等藏于此) -->
      <DetailPanel
        :selection="panelSelection"
        :node-assets="nodeAssets"
        :assets-loading="assetsLoading"
        @close="closePanel"
        @view-command="openCommandDialog"
      />
      <!-- 首次加载覆盖层:Vue Flow 保持挂载,避免 useVueFlow 实例失效 -->
      <div
        v-if="initialLoading"
        class="absolute inset-0 z-10 flex items-center justify-center bg-fill-2"
      >
        <n-spin size="large" />
      </div>
    </div>

    <!-- 部署详情抽屉:头部「详情」按钮展开;抽屉内删除成功后 removed → 退出详情视图回列表 -->
    <DeployDetailDrawer v-model:show="detailDrawerShow" :record="deployment" @removed="back" />
  </div>
</template>

<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
@import '@vue-flow/minimap/dist/style.css';
</style>

<style lang="scss">
/* 画布配色:全部走主题变量,明暗自适应;scoped 无法命中 Vue Flow 内部 DOM,故用全局块 + 前缀限定(同 node-pool TopologyView) */
.workflow-flow {
  .vue-flow__edge {
    transition: opacity 0.2s ease;
  }
  .vue-flow__edge.edge-static .vue-flow__edge-path {
    stroke: var(--c-line-strong);
  }
  .vue-flow__edge.edge-flow .vue-flow__edge-path {
    stroke: rgb(var(--n-primary-color));
  }
  .vue-flow__minimap {
    background: var(--c-fill-1);
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
  /* 供给中副本(扩容新份/未就绪):呼吸脉动提示进行中;颜色走主题变量明暗自适应 */
  .replica-provisioning {
    animation: replica-provisioning-pulse 1.6s ease-in-out infinite;
  }
  @keyframes replica-provisioning-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.55;
    }
  }
}
</style>
