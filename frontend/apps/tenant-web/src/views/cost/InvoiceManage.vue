<script setup lang="ts">
import { NTag } from 'naive-ui'
import { useTable } from '@repo/hooks'

/** 申请开票表单(静态) */
const invoiceForm = ref({
  amount: '',
  company: '',
  taxNo: '',
})

/** 开票记录(静态数据;接后端后把 request 换成接口调用) */
interface InvoiceRecord {
  id: number
  invoiceNo: string
  content: string
  amount: string
  status: '待开具' | '已开具' | '已取消'
  date: string
}
const invoiceStatusType: Record<InvoiceRecord['status'], 'warning' | 'success' | 'error'> = {
  待开具: 'warning',
  已开具: 'success',
  已取消: 'error',
}
const invoiceRecords: InvoiceRecord[] = [
  {
    id: 1,
    invoiceNo: 'INV-2026-07-001',
    content: '2026年7月消费发票',
    amount: '¥45.30',
    status: '待开具',
    date: '2026-07-03',
  },
  {
    id: 2,
    invoiceNo: 'INV-2026-07-001',
    content: '2026年6月消费发票',
    amount: '¥45.30',
    status: '已开具',
    date: '2026-07-03',
  },
  {
    id: 3,
    invoiceNo: 'INV-2026-07-001',
    content: '2026年5月消费发票',
    amount: '¥45.30',
    status: '已开具',
    date: '2026-07-03',
  },
  {
    id: 4,
    invoiceNo: 'INV-2026-07-001',
    content: '2026年4月消费发票',
    amount: '¥45.30',
    status: '已开具',
    date: '2026-07-03',
  },
  {
    id: 5,
    invoiceNo: 'INV-2026-07-001',
    content: '2026年3月消费发票',
    amount: '¥45.30',
    status: '已取消',
    date: '2026-07-03',
  },
]

/** 静态表格:一次性注入数据,搜索与分页切片交给 useTable 的 local 模式 */
const { LmTable, LmPage, setData } = useTable<InvoiceRecord>({
  request: async () => ({ data: [], total: 0 }),
  columns: [
    { title: '发票账号', key: 'invoiceNo', minWidth: 140 },
    { title: '发票内容', key: 'content', minWidth: 160 },
    { title: '金额', key: 'amount', minWidth: 90 },
    {
      title: '状态',
      key: 'status',
      minWidth: 100,
      render: (row) =>
        h(
          NTag,
          { size: 'small', type: invoiceStatusType[row.status], bordered: false },
          { default: () => row.status },
        ),
    },
    { title: '日期', key: 'date', minWidth: 110 },
    {
      title: '操作',
      key: 'action',
      align: 'right',
      minWidth: 90,
      render: (row) =>
        row.status === '已开具'
          ? h('span', { class: 'cursor-pointer text-primary' }, '下载')
          : h('span', { class: 'text-secondary' }, '待生成'),
    },
  ],
  rowKey: 'id',
  immediate: false,
  config: {
    pagination: { pageSize: 10, pageSizes: [10, 20, 50] },
    tableProps: { flexHeight: false },
    // local 模式:静态数据在前端完成过滤与分页,不发请求
    filter: {
      mode: 'local',
      fields: ['invoiceNo', 'content', 'amount', 'status', 'date'],
    },
  },
})
setData(invoiceRecords, invoiceRecords.length)
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard
      segmented
      title="申请开票"
    >
      <div class="flex flex-wrap items-center gap-[.75rem]">
        <n-input
          v-model:value="invoiceForm.amount"
          placeholder="开票金额"
          class="!w-[20rem]"
        />
        <n-input
          v-model:value="invoiceForm.company"
          placeholder="公司名称"
          class="!w-[20rem]"
        />
        <n-input
          v-model:value="invoiceForm.taxNo"
          placeholder="税号"
          class="!w-[20rem]"
        />
        <n-button type="primary"> 提交申请 </n-button>
      </div>
    </LmCard>

    <LmCard
      segmented
      title="开票记录"
    >
      <LmTableGroup>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty description="暂无开票记录" />
            </template>
          </LmTable>
        </template>
        <template #footer-right>
          <LmPage />
        </template>
      </LmTableGroup>
    </LmCard>
  </div>
</template>
