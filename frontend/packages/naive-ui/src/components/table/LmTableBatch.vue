<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import LmSelect from '../form/LmSelect.vue'
import { useLmTableContext } from './context'
import type { LmBatchAction, LmTableBatchSelectPayload } from './types'

defineOptions({ name: 'LmTableBatch', inheritAttrs: false })

interface Props {
  actions: LmBatchAction[]
  /** 批量操作下拉占位文案 */
  placeholder?: string
  /** 确认按钮文案 */
  confirmText?: string
}

const props = withDefaults(defineProps<Props>(), {
  placeholder: '批量操作',
  confirmText: '确定',
})

const emit = defineEmits<{
  select: [payload: LmTableBatchSelectPayload]
}>()

const ctx = useLmTableContext()

const selectedKeys = computed(() => ctx?.checkedRowKeys.value ?? [])
const hasRows = computed(() => (ctx?.data.value.length ?? 0) > 0)
const hasSelection = computed(() => selectedKeys.value.length > 0)

/** 当前页全部行 keys(与 NDataTable 表头全选同范围) */
const pageKeys = computed<Array<string | number>>(() => {
  if (!ctx) return []
  return ctx.data.value.map((row) => ctx.rowKey(row))
})

const selectedKeySet = computed(() => new Set(selectedKeys.value))

const isChecked = computed(() => {
  if (!hasRows.value) return false
  return pageKeys.value.every((k) => selectedKeySet.value.has(k))
})
const isIndeterminate = computed(() => {
  if (!hasRows.value || isChecked.value) return false
  return pageKeys.value.some((k) => selectedKeySet.value.has(k))
})

function toggleAll(checked: boolean) {
  if (!ctx) return
  if (checked) {
    ctx.setSelection(pageKeys.value)
  } else {
    ctx.clearSelection()
  }
}

const selectedAction = ref<string | null>(null)

const selectOptions = computed<SelectOption[]>(() =>
  props.actions.map((a) => ({
    label: a.label,
    value: a.value,
    disabled: a.disabled,
    class: a.danger ? 'text-error' : '',
  })),
)

const canConfirm = computed(() => hasSelection.value && !!selectedAction.value)

function onConfirm() {
  if (!canConfirm.value || !selectedAction.value) return
  const action = props.actions.find((a) => a.value === selectedAction.value)
  if (!action) return
  emit('select', {
    action: action.value,
    selectedKeys: [...selectedKeys.value],
    selectedRows: [...(ctx?.checkedRows.value ?? [])],
    refresh: (opts?: { keepPage?: boolean }) => ctx?.refresh(opts),
  })
  // 完成后自动清空选择(后续是否 refresh 由业务方决定)
  ctx?.clearSelection()
  selectedAction.value = null
}

// 选中被清空时重置待执行操作,避免残留
watch(hasSelection, (value) => {
  if (!value) selectedAction.value = null
})
</script>

<template>
  <div v-bind="$attrs" class="flex items-center gap-[.8rem]">
    <n-checkbox
      class="w-[4rem] flex justify-center"
      :checked="isChecked"
      :indeterminate="isIndeterminate"
      :disabled="!hasRows"
      @update:checked="toggleAll"
    />
    <LmSelect
      v-model:value="selectedAction"
      :options="selectOptions"
      :placeholder="placeholder"
      :disabled="!hasSelection"
      size="small"
      style="width: 160px"
    />
    <n-button size="small" type="primary" :disabled="!canConfirm" @click="onConfirm">
      {{ confirmText }}
    </n-button>
  </div>
</template>
