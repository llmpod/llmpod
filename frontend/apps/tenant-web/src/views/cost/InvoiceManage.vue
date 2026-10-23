<script setup lang="ts">
import { NTag, type DataTableColumns } from 'naive-ui'

/** 申请开票表单(静态) */
const invoiceForm = ref({
  amount: '',
  company: '',
  taxNo: '',
})

/** 开票记录(静态) */
interface InvoiceRecord {
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
    invoiceNo: 'INV-2026-07-001',
    content: '2026年7月消费发票',
    amount: '¥45.30',
    status: '待开具',
    date: '2026-07-03',
  },
  {
    invoiceNo: 'INV-2026-07-001',
    content: '2026年6月消费发票',
    amount: '¥45.30',
    status: '已开具',
    date: '2026-07-03',
  },
  {
    invoiceNo: 'INV-2026-07-001',
    content: '2026年5月消费发票',
    amount: '¥45.30',
    status: '已开具',
    date: '2026-07-03',
  },
  {
    invoiceNo: 'INV-2026-07-001',
    content: '2026年4月消费发票',
    amount: '¥45.30',
    status: '已开具',
    date: '2026-07-03',
  },
  {
    invoiceNo: 'INV-2026-07-001',
    content: '2026年3月消费发票',
    amount: '¥45.30',
    status: '已取消',
    date: '2026-07-03',
  },
]
const invoiceColumns: DataTableColumns<InvoiceRecord> = [
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
]
const invoicePage = ref(1)
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard segmented title="申请开票">
      <div class="flex flex-wrap items-center gap-[.75rem]">
        <n-input v-model:value="invoiceForm.amount" placeholder="开票金额" class="!w-[20rem]" />
        <n-input v-model:value="invoiceForm.company" placeholder="公司名称" class="!w-[20rem]" />
        <n-input v-model:value="invoiceForm.taxNo" placeholder="税号" class="!w-[20rem]" />
        <n-button type="primary"> 提交申请 </n-button>
      </div>
    </LmCard>

    <LmCard segmented title="开票记录">
      <n-data-table :columns="invoiceColumns" :data="invoiceRecords" :bordered="false" />
      <div class="mt-[.75rem] flex items-center justify-end gap-[.75rem]">
        <n-pagination
          v-model:page="invoicePage"
          :item-count="5"
          :page-size="10"
          show-size-picker
          :page-sizes="[10, 20, 50]"
        >
          <template #suffix> 共 5 条 </template>
        </n-pagination>
      </div>
    </LmCard>
  </div>
</template>
