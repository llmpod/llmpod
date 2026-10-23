<script setup lang="ts">
import { NTag, type DataTableColumns } from 'naive-ui'

/** 历史账单(静态) */
interface BillRecord {
  period: string
  amount: string
  status: '待支付' | '已支付'
  dueDate: string
}
const billRecords: BillRecord[] = [
  { period: '2026-07-03', amount: '¥45.30', status: '待支付', dueDate: '2026-07-03' },
  { period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
  { period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
  { period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
  { period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
]
const billColumns: DataTableColumns<BillRecord> = [
  { title: '账期', key: 'period', minWidth: 110 },
  { title: '金额', key: 'amount', minWidth: 100 },
  {
    title: '状态',
    key: 'status',
    minWidth: 100,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.status === '已支付' ? 'success' : 'warning', bordered: false },
        { default: () => row.status },
      ),
  },
  { title: '付款截止日', key: 'dueDate', minWidth: 120 },
  {
    title: '操作',
    key: 'action',
    align: 'right',
    minWidth: 90,
    render: (row) =>
      h(
        'span',
        { class: 'cursor-pointer text-primary' },
        row.status === '待支付' ? '立即付款' : '下载',
      ),
  },
]
const billPage = ref(1)
</script>

<template>
  <LmCard>
    <n-button type="primary" class="mb-[.75rem]"> 历史账单 </n-button>
    <n-data-table :columns="billColumns" :data="billRecords" :bordered="false" />
    <div class="mt-[.75rem] flex items-center justify-end gap-[.75rem]">
      <n-pagination
        v-model:page="billPage"
        :item-count="5"
        :page-size="10"
        show-size-picker
        :page-sizes="[10, 20, 50]"
      >
        <template #suffix> 共 5 条 </template>
      </n-pagination>
    </div>
  </LmCard>
</template>
