import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import { useTable } from '@repo/hooks'
import GpuRuntimeExpand from './GpuRuntimeExpand.vue'
import { useStore, gpuStatusMap, type Gpu } from './useStore'

export function useController() {
  const { request } = useStore()

  const { LmTable, LmPage, LmRefresh, LmColumn } = useTable<Gpu, Record<string, never>>({
    request,
    columns: [
      // 行展开:GPU 实时指标摘要(核心/显存利用率 + 温度 + 功耗);
      // runtime 为 null(节点离线或心跳未上报)时组件内渲染空态,数据随列表返回不单独请求
      {
        type: 'expand',
        renderExpand: (row: Gpu) =>
          h(GpuRuntimeExpand, { runtime: row.runtime, totalVramGb: row.total_vram_gb }),
      },
      { title: '所属节点', key: 'nodeHost', minWidth: 140, hideable: false },
      { title: 'GPU 序号', key: 'index', width: 90 },
      { title: '型号', key: 'name', minWidth: 160 },
      { title: '架构', key: 'arch', width: 120 },
      {
        title: '显存',
        key: 'total_vram_gb',
        width: 100,
        render: (row) => `${row.total_vram_gb}GB`,
      },
      {
        title: '驱动版本',
        key: 'driver_version',
        minWidth: 140,
        render: (row) =>
          row.driver_version
            ? row.driver_version
            : h('span', { class: 'text-error' }, '未安装'),
      },
      {
        title: '状态',
        key: 'status',
        width: 110,
        render: (row) => {
          const conf = gpuStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
      request: { pageSizeField: 'page_size' },
    },
  })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
  }
}
