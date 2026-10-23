<script setup lang="ts">
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import { useTable } from '@repo/hooks'
import {
  useStore,
  transactionTypeMap,
  formatAmount,
  amountTone,
  formatDateTime,
  formatDecimal,
  formatTokens,
  type BalanceTransactionData,
} from './useStore'

const { request } = useStore()

const { LmTable, LmPage, LmRefresh, LmColumn } = useTable<BalanceTransactionData>({
  request,
  columns: [
    {
      title: '时间',
      key: 'created_at',
      width: 150,
      hideable: false,
      render: (row) => formatDateTime(row.created_at),
    },
    {
      title: '类型',
      key: 'type',
      width: 90,
      render: (row) => {
        const conf = transactionTypeMap[row.type] ?? { label: row.type, type: 'default' }
        return h(
          NTag,
          { type: conf.type, size: 'small', bordered: false },
          { default: () => conf.label },
        )
      },
    },
    // 原「关联信息」把模型/Key/请求 ID 三层挤在一个单元格里:模型名被埋没,
    // 窄屏卡片也挑不出主标题。拆成独立列,桌面可比较、卡片可指认。
    {
      title: '模型',
      key: 'route_name',
      minWidth: 160,
      render: (row) => row.route_name || '—',
    },
    {
      title: 'API Key',
      key: 'api_key_name',
      minWidth: 130,
      render: (row) => row.api_key_name || '—',
    },
    {
      title: '请求 ID',
      key: 'request_id',
      minWidth: 150,
      render: (row) =>
        row.request_id
          ? h(
              'span',
              { class: 'font-mono text-secondary', title: row.request_id },
              `${row.request_id.slice(0, 8)}…`,
            )
          : '—',
    },
    {
      title: '变动金额(元)',
      key: 'amount',
      width: 130,
      align: 'right',
      render: (row) =>
        h(
          'span',
          {
            class: `font-mono tabular-nums ${amountTone(row.amount) === 'success' ? 'text-success' : 'text-error'}`,
          },
          formatAmount(row.amount),
        ),
    },
    {
      title: '变动后余额(元)',
      key: 'balance_after',
      width: 140,
      align: 'right',
      render: (row) => h('span', { class: 'font-mono tabular-nums' }, formatDecimal(row.balance_after)),
    },
    {
      title: 'Token',
      key: 'tokens',
      width: 110,
      align: 'right',
      // 后端消费流水 meta 只存 prompt_tokens/completion_tokens(见 _billing_meta),前端合计展示
      render: (row) =>
        h('span', { class: 'font-mono tabular-nums' }, formatTokens(row.meta)),
    },
  ],
  rowKey: 'id',
  config: {
    pagination: { pageSize: 20 },
    tableProps: { flexHeight: false },
    request: { pageSizeField: 'page_size' },
    // 窄屏卡片:模型名做主标题,金额提到卡首,时间/余额/Token 对齐成列,
    // Key 与请求 ID 收进「展开 2 项」
    card: {
      titleKey: 'route_name',
      tagKey: 'type',
      emphasisKey: 'amount',
      fieldKeys: ['created_at', 'balance_after', 'tokens'],
    },
  },
})
</script>

<template>
  <LmCard>
    <LmTableGroup>
      <template #header-right>
        <LmRefresh />
        <LmColumn />
      </template>
      <template #content>
        <LmTable>
          <template #empty>
            <LmTableEmpty description="暂无流水记录,充值与调用消费都会在这里展示" />
          </template>
        </LmTable>
      </template>
      <template #footer-right>
        <LmPage />
      </template>
    </LmTableGroup>
  </LmCard>
</template>
