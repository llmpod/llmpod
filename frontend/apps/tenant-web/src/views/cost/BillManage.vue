<script setup lang="ts">
import { NTag } from 'naive-ui'
import { useTable } from '@repo/hooks'

/** 历史账单(静态数据;接后端后把 request 换成接口调用) */
interface BillRecord {
  id: number
  period: string
  amount: string
  status: '待支付' | '已支付'
  dueDate: string
}
const billRecords: BillRecord[] = [
  { id: 1, period: '2026-07-03', amount: '¥45.30', status: '待支付', dueDate: '2026-07-03' },
  { id: 2, period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
  { id: 3, period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
  { id: 4, period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
  { id: 5, period: '2026-07-03', amount: '¥45.30', status: '已支付', dueDate: '2026-07-03' },
]

/** 静态表格:一次性注入数据,搜索与分页切片交给 useTable 的 local 模式 */
const { LmTable, LmPage, setData } = useTable<BillRecord>({
  request: async () => ({ data: [], total: 0 }),
  columns: [
    { title: '账期', key: 'period', minWidth: 110 },
    { title: '金额', key: 'amount', minWidth: 100 },
    {
      title: '状态',
      key: 'status',
      minWidth: 100,
      render: (row) =>
        h(
          NTag,
          {
            size: 'small',
            type: row.status === '已支付' ? 'success' : 'warning',
            bordered: false,
          },
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
  ],
  rowKey: 'id',
  immediate: false,
  config: {
    pagination: { pageSize: 10, pageSizes: [10, 20, 50] },
    tableProps: { flexHeight: false },
    // local 模式:静态数据在前端完成过滤与分页,不发请求
    filter: { mode: 'local', fields: ['period', 'amount', 'status', 'dueDate'] },
  },
})
setData(billRecords, billRecords.length)
</script>

<template>
  <LmCard>
    <n-button
      type="primary"
      class="mb-[.75rem]"
    >
      历史账单
    </n-button>
    <LmTableGroup>
      <template #content>
        <LmTable>
          <template #empty>
            <LmTableEmpty description="暂无历史账单" />
          </template>
        </LmTable>
      </template>
      <template #footer-right>
        <LmPage />
      </template>
    </LmTableGroup>
  </LmCard>
</template>
