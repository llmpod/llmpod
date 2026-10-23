<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { CubeOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import { modelBrandIcon } from './brandIcons'
import type { FlowModel } from '../useStore'

const props = defineProps<NodeProps>()
const model = computed(() => props.data.record as FlowModel)
const detail = computed(() => Boolean(props.data.detail))
/** 模型家族品牌图标(Qwen/DeepSeek 等),未命中回退通用立方体图标 */
const icon = computed(() => modelBrandIcon(model.value.name, model.value.category) ?? CubeOutline)
</script>

<template>
  <NodeCard :icon="icon" :title="model.name" :subtitle="model.category">
    <template v-if="detail">
      <div class="flex justify-between">
        <span class="text-secondary">格式</span><span>{{ model.format ?? '--' }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">大小</span
        ><span>{{ model.sizeGb === null ? '--' : `${model.sizeGb}GB` }}</span>
      </div>
    </template>
  </NodeCard>
</template>
