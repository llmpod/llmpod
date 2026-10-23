<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NIcon } from 'naive-ui'
import { Refresh } from '@vicons/ionicons5'
import { useLmTableContext } from './context'

defineOptions({ name: 'LmTableRefresh', inheritAttrs: false })

interface Props {
  /** 按钮文字(button 模式生效) */
  text?: string
  /** 显式覆盖加载态；未传时取上下文 loading */
  loading?: boolean
  disabled?: boolean
  /** button: 图标+文字；icon: 纯图标圆形按钮 */
  variant?: 'button' | 'icon'
}

const props = withDefaults(defineProps<Props>(), {
  text: '刷新',
  variant: 'icon',
})

const emit = defineEmits<{ click: [] }>()

const ctx = useLmTableContext()

const loading = computed(() => props.loading ?? ctx?.loading.value ?? false)
// 请求期间禁用，防止重复触发
const disabled = computed(() => props.disabled || loading.value)

async function handleClick() {
  if (disabled.value) return
  if (ctx?.refresh) {
    await ctx.refresh()
    return
  }
  // 无上下文(脱离 useTable)：交由父组件处理
  emit('click')
}
</script>

<template>
  <n-button
    v-bind="$attrs"
    :disabled="disabled"
    :title="variant === 'icon' ? text : undefined"
    @click="handleClick"
  >
    <template #icon>
      <n-icon :class="{ 'lm-table-refresh__icon--spinning': loading }"><Refresh /></n-icon>
    </template>
    <template v-if="variant === 'button'">
      <slot>{{ text }}</slot>
    </template>
  </n-button>
</template>

<style scoped>
.lm-table-refresh__icon--spinning {
  animation: lm-table-refresh-spin 0.8s linear infinite;
}

@keyframes lm-table-refresh-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
