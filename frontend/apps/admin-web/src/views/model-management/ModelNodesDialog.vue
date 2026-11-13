<script setup lang="ts">
import { h } from 'vue'
import { NTag } from 'naive-ui'
import { useDialog, useOperate, useTable } from '@repo/hooks'
import {
  formatBytes,
  formatDateTime,
  MODEL_NODE_STATUS_LABEL,
  MODEL_NODE_STATUS_TAG,
  NODE_STATUS_LABEL,
  NODE_STATUS_TAG,
  useStore,
  type ModelNodeCopy,
} from './useStore'
import DeleteNodeCopyDialog from './DeleteNodeCopyDialog.vue'

/** 目标模型:展示其已下载的节点副本,并可逐节点清理 */
const props = defineProps<{ modelId: number; modelName: string }>()

const { nodes, subscribe } = useStore()

/** 节点展示名:主机名(IP),缺失回退节点 ID */
function nodeLabel(row: ModelNodeCopy): string {
  const name = row.hostname || row.node_id
  return row.ip ? `${name} (${row.ip})` : name
}

/** 清理节点副本:默认仅移除记录,可选删除磁盘文件(在子弹窗内二次选择并确认) */
function openDelete(row: ModelNodeCopy) {
  useDialog({
    title: '删除节点副本',
    component: DeleteNodeCopyDialog,
    area: 42,
    btn: { confirmText: '确认删除', cancelText: '取消' },
    props: { modelId: props.modelId, node: row },
  })
}

/** 状态标签:节点状态与模型状态各自独立判定、分列展示 */
function renderStatus(
  label: Record<string, string>,
  tag: Record<string, 'success' | 'warning' | 'error' | 'default'>,
  value: string | null | undefined,
) {
  return h(
    NTag,
    { size: 'small', bordered: false, type: tag[value ?? ''] ?? 'default' },
    { default: () => label[value ?? ''] ?? value ?? '--' },
  )
}

const { LmTable, LmRefresh, total, refresh } = useTable<ModelNodeCopy>({
  request: async () => {
    const list = await nodes(props.modelId)
    return { data: list, total: list.length }
  },
  columns: [
    {
      title: '节点',
      key: 'node_id',
      minWidth: 160,
      hideable: false,
      render: (row) => nodeLabel(row),
    },
    {
      title: '路径',
      key: 'local_dir',
      minWidth: 200,
      render: (row) => row.local_dir ?? '--',
    },
    {
      title: '节点状态',
      key: 'node_status',
      render: (row) => renderStatus(NODE_STATUS_LABEL, NODE_STATUS_TAG, row.node_status),
    },
    {
      title: '模型状态',
      key: 'model_status',
      render: (row) => renderStatus(MODEL_NODE_STATUS_LABEL, MODEL_NODE_STATUS_TAG, row.model_status),
    },
    {
      title: '大小',
      key: 'bytes_total',
      render: (row) => formatBytes(row.bytes_total),
    },
    {
      title: '最近校验时间',
      key: 'last_checked_at',
      render: (row) => formatDateTime(row.last_checked_at ?? null),
    },
    useOperate<ModelNodeCopy>([{ title: '删除', danger: true, onClick: openDelete }], {
      width: 80,
    }),
  ],
  rowKey: 'node_id',
  config: { pagination: false, tableProps: { flexHeight: false } },
})

// 清理成功后 store notify → 弹窗内列表同步刷新(列表页已下载节点数也随之更新)
const unsubscribe = subscribe(() => {
  void refresh()
})
onBeforeUnmount(unsubscribe)
</script>

<template>
  <div class="flex flex-col gap-[.8rem] pt-[.5rem]">
    <div class="flex items-center justify-between gap-[1.2rem]">
      <span class="text-small text-secondary">
        模型「{{ modelName }}」已下载到 {{ total }} 台节点
      </span>
      <LmRefresh />
    </div>
    <LmTable :max-height="380">
      <template #empty>
        <span class="text-secondary">该模型尚未下载到任何节点</span>
      </template>
    </LmTable>
  </div>
</template>