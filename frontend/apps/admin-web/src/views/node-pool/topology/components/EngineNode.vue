<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { CogOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import { engineBrandIcon } from './brandIcons'
import type { EngineNodeData } from '../useStore'

const props = defineProps<NodeProps>()
const engine = computed(() => props.data.record as EngineNodeData)
const detail = computed(() => Boolean(props.data.detail))
/** 版本取自镜像 tag,缺省时只显示名称 */
const title = computed(() =>
  engine.value.version === '--' ? engine.value.name : `${engine.value.name} ${engine.value.version}`,
)
/** 引擎品牌图标(vLLM/SGLang 等),未命中回退通用齿轮图标 */
const icon = computed(() => engineBrandIcon(engine.value.name, engine.value.key) ?? CogOutline)
</script>

<template>
  <NodeCard
    :icon="icon"
    :title="title"
    subtitle="推理引擎"
  >
    <div v-if="detail" class="flex justify-between">
      <span class="text-secondary">关联部署</span><span>{{ engine.deployCount }} 个</span>
    </div>
  </NodeCard>
</template>
