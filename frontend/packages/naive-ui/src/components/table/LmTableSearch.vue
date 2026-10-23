<script setup lang="ts">
import { ref, watch } from 'vue'
import { NInput, NIcon } from 'naive-ui'
import { SearchOutline } from '@vicons/ionicons5'

defineOptions({ name: 'LmTableSearch', inheritAttrs: false })

interface Props {
  /** 输入值(v-model) */
  modelValue?: string
  /** 防抖毫秒;<= 0 表示仅回车/图标触发 */
  debounce?: number
  /** 占位文案 */
  placeholder?: string
  /** 是否可清空 */
  clearable?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  modelValue: '',
  debounce: 300,
  placeholder: '请输入关键字',
  clearable: true,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  search: [value: string]
}>()

const inner = ref(props.modelValue)

// 外部受控变更(如 reset 清空)同步到内部输入框
watch(
  () => props.modelValue,
  (value) => {
    if (value !== inner.value) inner.value = value
  },
)

let timer: ReturnType<typeof setTimeout> | null = null

function commit(value: string) {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
  emit('update:modelValue', value)
  emit('search', value)
}

function onInput(value: string) {
  inner.value = value
  if (props.debounce <= 0) return
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => commit(value), props.debounce)
}

function onEnter() {
  commit(inner.value)
}

function onClear() {
  inner.value = ''
  commit('')
}
</script>

<template>
  <n-input
    v-bind="$attrs"
    v-model:value="inner"
    :placeholder="placeholder"
    :clearable="clearable"
    @input="onInput"
    @keyup.enter="onEnter"
    @clear="onClear"
  >
    <template #suffix>
      <div class="search-warpper">
        <n-icon class="cursor-pointer" @click="onEnter">
          <SearchOutline />
        </n-icon>
      </div>
    </template>
  </n-input>
</template>

<style scoped>
:deep(.n-input-wrapper) {
  padding-right: 0;
}
.search-warpper {
  @apply w-[3.2rem] h-full flex items-center justify-center bg-[#e9e9e9] dark:bg-[#2a2a2a] cursor-pointer;
}
</style>
