<script setup lang="ts">
import { ref } from 'vue'
import { NDataTable } from 'naive-ui'
import type {
  DataTableColumn,
  DataTableCreateRowKey,
  DataTableInst,
  DataTableRowData,
} from 'naive-ui'

defineOptions({ name: 'LmTable', inheritAttrs: false })

interface Props<Row extends DataTableRowData = DataTableRowData> {
  /** 表格数据；未传时由 n-data-table 用自身默认 */
  data?: Row[]
  /** 列定义；支持原生 Naive UI 列 */
  columns?: DataTableColumn<Row>[]
  /** 加载态 */
  loading?: boolean
  /** 行唯一标识取值函数；不得使用数组下标 */
  rowKey?: DataTableCreateRowKey<Row>
  /** 受控选中行键 */
  checkedRowKeys?: Array<string | number>
}

defineProps<Props>()

const tableRef = ref<DataTableInst | null>(null)

// 暴露 DataTableInst 全部方法，对齐 useTable 的 tableRef: Ref<DataTableInst | null> 契约
defineExpose({
  filter: (...args: Parameters<DataTableInst['filter']>) => tableRef.value?.filter(...args),
  filters: (...args: Parameters<DataTableInst['filters']>) => tableRef.value?.filters(...args),
  clearFilters: () => tableRef.value?.clearFilters(),
  clearSorter: () => tableRef.value?.clearSorter(),
  page: (...args: Parameters<DataTableInst['page']>) => tableRef.value?.page(...args),
  sort: (...args: Parameters<DataTableInst['sort']>) => tableRef.value?.sort(...args),
  scrollTo: (...args: Parameters<DataTableInst['scrollTo']>) => tableRef.value?.scrollTo(...args),
  downloadCsv: (...args: Parameters<DataTableInst['downloadCsv']>) =>
    tableRef.value?.downloadCsv(...args),
  /** @deprecated 保留兼容，代理到 n-data-table 的 clearFilter */
  clearFilter: () => tableRef.value?.clearFilter(),
})
</script>

<template>
  <!-- scrollbar-props 默认 trigger:'none':小屏列溢出横向滚动时滚动条常驻,
       不再"滚动/悬停时短暂出现后淡出"(触屏无从感知可横向滚动);不溢出时
       naive 内部 needXBar 仍为 false,不渲染滚动条。业务透传值优先于默认。 -->
  <n-data-table
    ref="tableRef"
    v-bind="$attrs"
    :data="data"
    :columns="columns"
    :loading="loading"
    :row-key="rowKey"
    :checked-row-keys="checkedRowKeys"
    :pagination="false"
    :bordered="false"
    :scrollbar-props="{
      trigger: 'none',
      ...($attrs['scrollbar-props'] ?? $attrs.scrollbarProps ?? {}),
    }"
  >
    <template v-for="(_, name) in $slots" #[name]="slotData" :key="name">
      <slot :name="name" v-bind="slotData ?? {}" />
    </template>
  </n-data-table>
</template>
