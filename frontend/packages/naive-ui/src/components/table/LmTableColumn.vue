<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { NButton, NCheckbox, NCheckboxGroup, NEmpty, NIcon, NPopover } from 'naive-ui'
import { GridOutline } from '@vicons/ionicons5'
import type { DataTableColumn } from 'naive-ui'
import { useLmTableContext } from './context'
import type { ColumnPersistenceMap } from './types'

defineOptions({ name: 'LmTableColumn', inheritAttrs: false })

interface Props {
  /** 覆盖上下文的持久化 key；为空则不持久化 */
  storageKey?: string
  text?: string
  resetable?: boolean
  /** 列拖拽排序(本次仅预留 API，未实现) */
  draggable?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  text: '列设置',
  resetable: true,
  draggable: false,
})

const emit = defineEmits<{
  change: [map: Record<string, boolean>]
}>()

const ctx = useLmTableContext()

const storageKey = computed(() => props.storageKey ?? ctx?.columnStorageKey)

/** 联合类型列的安全元数据访问(避免直接 any) */
function colMeta(col: DataTableColumn): Record<string, unknown> {
  return col as unknown as Record<string, unknown>
}

function colKey(col: DataTableColumn): string {
  return String(colMeta(col).key)
}

function colTitle(col: DataTableColumn): string {
  const t = colMeta(col).title
  return typeof t === 'string' ? t : colKey(col)
}

/** 判定列是否可隐藏：排除选择/展开列、操作列、锁定列、无 key 列 */
function isHideable(col: DataTableColumn): boolean {
  const c = colMeta(col)
  if (c.type === 'selection' || c.type === 'expand') return false
  // 操作列约定标记(由 hooks 层 useOperate 打标)
  if (c.__lmOperate === true) return false
  if (c.key == null) return false
  return c.hideable !== false
}

/** 判定列是否为锁定列(hideable:false 且有 key):面板中显示但 checkbox 禁用 */
function isLocked(col: DataTableColumn): boolean {
  const c = colMeta(col)
  return c.hideable === false && c.key != null
}

/** 面板展示列 = 可隐藏列 + 锁定列(保持原始列顺序) */
const panelColumns = computed(() =>
  (ctx?.rawColumns.value ?? []).filter((col) => isHideable(col) || isLocked(col)),
)

const hideableColumns = computed(() => (ctx?.rawColumns.value ?? []).filter(isHideable))

const visibleKeys = computed<string[]>({
  get() {
    const map = ctx?.columnVisibleMap.value ?? {}
    return panelColumns.value.filter((c) => map[colKey(c)] !== false).map(colKey)
  },
  set(keys: string[]) {
    if (!ctx) return
    const selected = new Set(keys)
    // 锁定列始终强制可见,不受用户操作影响
    panelColumns.value.forEach((c) => {
      const k = colKey(c)
      ctx.setColumnVisible(k, isLocked(c) ? true : selected.has(k))
    })
    persist(storageKey.value, ctx.columnVisibleMap.value)
    emit('change', { ...ctx.columnVisibleMap.value })
  },
})

function loadPersisted(key: string | undefined): ColumnPersistenceMap {
  if (!key) return {}
  try {
    return JSON.parse(localStorage.getItem(key) || '{}') as ColumnPersistenceMap
  } catch {
    return {}
  }
}

/** 仅持久化 visible 等展示元数据，不含 render/函数 */
function persist(key: string | undefined, visibleMap: Record<string, boolean>) {
  if (!key) return
  const meta: ColumnPersistenceMap = {}
  for (const [k, visible] of Object.entries(visibleMap)) {
    meta[k] = { visible }
  }
  try {
    localStorage.setItem(key, JSON.stringify(meta))
  } catch {
    // quota / 隐私模式，忽略
  }
}

// 挂载时：加载本地偏好并合并到上下文(以最新列定义为准，已删除列的偏好自然丢弃)
onMounted(() => {
  if (!ctx) return
  const persisted = loadPersisted(storageKey.value)
  // 仅对可隐藏列应用持久化偏好;锁定列不受 localStorage 脏数据影响
  hideableColumns.value.forEach((c) => {
    const k = colKey(c)
    if (persisted[k]?.visible === false) {
      ctx.setColumnVisible(k, false)
    }
  })
})

function handleReset() {
  ctx?.resetColumnSetting()
  if (storageKey.value) {
    try {
      localStorage.removeItem(storageKey.value)
    } catch {
      // 忽略
    }
  }
  emit('change', {})
}
</script>

<template>
  <n-popover v-bind="$attrs" trigger="click">
    <template #trigger>
      <n-button :title="text">
        <template #icon>
          <n-icon><GridOutline /></n-icon>
        </template>
      </n-button>
    </template>
    <div class="flex flex-col gap-[.8rem] py-[.4rem]">
      <n-checkbox-group v-if="panelColumns.length" v-model:value="visibleKeys">
        <div class="flex flex-col gap-[.8rem]">
          <n-checkbox
            v-for="col in panelColumns"
            :key="colKey(col)"
            :value="colKey(col)"
            :label="colTitle(col)"
            :disabled="isLocked(col)"
          />
        </div>
      </n-checkbox-group>
      <n-empty v-else size="small" description="暂无可配置列" />
      <n-button v-if="resetable" size="small" block @click="handleReset"> 重置 </n-button>
    </div>
  </n-popover>
</template>
