<script setup lang="ts">
import { ArrowBackOutline } from '@vicons/ionicons5'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { useController } from './useController'
import { POLL_INTERVAL_MS } from './useStore'
import AggregateNode from './components/AggregateNode.vue'
import ClusterNode from './components/ClusterNode.vue'
import DeployNode from './components/DeployNode.vue'
import DetailPanel from './components/DetailPanel.vue'
import EngineNode from './components/EngineNode.vue'
import GpuNode from './components/GpuNode.vue'
import ModelNode from './components/ModelNode.vue'
import NodeNode from './components/NodeNode.vue'

const props = defineProps<{ clusterId?: number; clusterName?: string }>()
const emit = defineEmits<{ exit: [] }>()

const {
  clusterOptions,
  currentClusterId,
  mode,
  modeOptions,
  nodes,
  displayEdges,
  selection,
  loading,
  isEmpty,
  closePanel,
  jumpToManage,
} = useController({
  clusterId: props.clusterId,
  clusterName: props.clusterName,
  onExit: () => emit('exit'),
})
</script>

<template>
  <LmCard>
    <div class="flex items-center gap-[1.6rem] mb-[1.2rem] flex-wrap">
      <n-button size="small" @click="emit('exit')">
        <template #icon>
          <n-icon :component="ArrowBackOutline" />
        </template>
        返回
      </n-button>
      <LmSelect
        v-model:value="currentClusterId"
        :options="clusterOptions"
        class="w-[18rem]"
        size="small"
      />
      <n-radio-group v-model:value="mode" size="small">
        <n-radio-button v-for="item in modeOptions" :key="item.value" :value="item.value">
          {{ item.label }}
        </n-radio-button>
      </n-radio-group>
      <div class="ml-auto flex items-center gap-[1.2rem] text-xs text-secondary">
        <span class="flex items-center gap-[.4rem]">
          <i class="w-[.8rem] h-[.8rem] rounded-full bg-success inline-block" />在线/运行
        </span>
        <span class="flex items-center gap-[.4rem]">
          <i class="w-[.8rem] h-[.8rem] rounded-full bg-info inline-block" />繁忙
        </span>
        <span class="flex items-center gap-[.4rem]">
          <i class="w-[.8rem] h-[.8rem] rounded-full bg-warning inline-block" />维护/部署中
        </span>
        <span class="flex items-center gap-[.4rem]">
          <i class="w-[.8rem] h-[.8rem] rounded-full bg-error inline-block" />离线/停止
        </span>
        <span class="flex items-center gap-[.4rem]">
          <i class="w-[2rem] h-[.2rem] bg-primary inline-block" />推理链路
        </span>
        <span class="flex items-center gap-[.4rem]">
          <i class="w-[.8rem] h-[.8rem] rounded-full bg-primary inline-block animate-pulse" />
          每 {{ POLL_INTERVAL_MS / 1000 }} 秒自动刷新
        </span>
      </div>
    </div>
    <div
      class="relative h-[calc(100vh-20rem)] min-h-[48rem] rounded-lg border border-line overflow-hidden"
    >
      <VueFlow
        :nodes="nodes"
        :edges="displayEdges"
        class="workflow-flow bg-fill-2"
        fit-view-on-init
        :nodes-draggable="false"
        :nodes-connectable="false"
        :min-zoom="0.2"
        :max-zoom="1.5"
      >
        <Background :gap="24" />
        <Controls position="bottom-left" />
        <MiniMap pannable zoomable />
        <template #node-cluster="nodeProps"><ClusterNode v-bind="nodeProps" /></template>
        <template #node-node="nodeProps"><NodeNode v-bind="nodeProps" /></template>
        <template #node-gpu="nodeProps"><GpuNode v-bind="nodeProps" /></template>
        <template #node-deploy="nodeProps"><DeployNode v-bind="nodeProps" /></template>
        <template #node-engine="nodeProps"><EngineNode v-bind="nodeProps" /></template>
        <template #node-model="nodeProps"><ModelNode v-bind="nodeProps" /></template>
        <template #node-aggregate="nodeProps"><AggregateNode v-bind="nodeProps" /></template>
      </VueFlow>
      <DetailPanel :selection="selection" @close="closePanel" @jump="jumpToManage" />
      <!-- 加载/空态覆盖层:Vue Flow 保持挂载,避免 useVueFlow 实例失效 -->
      <div v-if="loading" class="absolute inset-0 z-10 flex items-center justify-center bg-fill-2">
        <n-spin size="large" />
      </div>
      <div v-else-if="isEmpty" class="absolute inset-0 z-10 flex items-center justify-center bg-fill-2">
        <n-empty description="暂无集群拓扑数据" />
      </div>
    </div>
  </LmCard>
</template>

<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
@import '@vue-flow/minimap/dist/style.css';
</style>

<style lang="scss">
/* 画布配色:全部走主题变量,明暗自适应;scoped 无法命中 Vue Flow 内部 DOM,故用全局块 + 前缀限定 */
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
  .vue-flow__edge.edge-dim {
    opacity: 0.15;
  }
  .vue-flow__edge.edge-hot .vue-flow__edge-path {
    stroke: rgb(var(--n-primary-color));
    stroke-width: 2;
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
}
</style>
