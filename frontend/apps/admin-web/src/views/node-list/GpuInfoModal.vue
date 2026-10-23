<script setup lang="ts">
import { gpuStatusMap, type ComputeNode } from './useStore'

const props = defineProps<{
  node: ComputeNode | null
}>()

/** GPU 列表来自列表行聚合数据 */
const gpus = computed(() => props.node?.gpus ?? [])
</script>

<template>
  <div
    v-if="gpus.length"
    class="flex flex-col gap-[1.6rem]"
  >
    <div
      v-for="g in gpus"
      :key="g.id"
      class="flex flex-col gap-[0.8rem]"
    >
      <div class="gpu-title">
        GPU {{ g.index }}
        <n-tag
          size="small"
          :type="(gpuStatusMap[g.status] ?? { label: g.status, type: 'default' }).type"
          :bordered="false"
        >
          {{ (gpuStatusMap[g.status] ?? { label: g.status, type: 'default' }).label }}
        </n-tag>
      </div>
      <div class="info-row">
        <span class="info-row__label">型号</span>
        <span>{{ g.name }}</span>
      </div>
      <div class="info-row">
        <span class="info-row__label">架构</span>
        <span>{{ g.arch }}</span>
      </div>
      <div class="info-row">
        <span class="info-row__label">显存</span>
        <span>{{ g.total_vram_gb }}GB</span>
      </div>
      <div class="info-row">
        <span class="info-row__label">驱动版本</span>
        <span :class="g.driver_version ? '' : 'text-error'">
          {{ g.driver_version ?? '未安装' }}
        </span>
      </div>
    </div>
  </div>
  <n-empty
    v-else
    description="该节点暂无 GPU"
    class="py-[4rem]"
  />
</template>

<style scoped lang="scss">
.gpu-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.info-row {
  display: flex;
  align-items: baseline;
  gap: 8px;

  &__label {
    font-weight: 600;
    white-space: nowrap;

    &::after {
      content: ':';
    }
  }
}
</style>
