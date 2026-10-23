<script setup lang="ts">
import { NButton, NEmpty } from 'naive-ui'

defineOptions({ name: 'LmTableEmpty' })

interface Props {
  /** 空态描述文案 */
  description?: string
  /** 引导按钮文案;不传则不渲染按钮 */
  actionText?: string
  /** NEmpty 尺寸 */
  size?: 'small' | 'medium' | 'large' | 'huge'
}

withDefaults(defineProps<Props>(), {
  description: '暂无数据',
  actionText: '',
  size: 'medium',
})

const emit = defineEmits<{
  action: []
}>()
</script>

<template>
  <n-empty :size="size" :description="description" class="py-[3.2rem]">
    <template v-if="$slots.icon" #icon>
      <slot name="icon" />
    </template>
    <template v-if="$slots.default || actionText" #extra>
      <slot>
        <n-button v-if="actionText" type="primary" @click="emit('action')">
          {{ actionText }}
        </n-button>
      </slot>
    </template>
  </n-empty>
</template>
