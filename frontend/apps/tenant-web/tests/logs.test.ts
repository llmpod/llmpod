// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/views/logs/AuditDetailDrawer.vue', () => ({ default: {} }))
vi.mock('@/api/audit-log', () => ({ listTenantAuditLogs: vi.fn() }))

import { listTenantAuditLogs } from '@/api/audit-log'
import {
  actionFilterOptions,
  actionMap,
  formatDateTime,
  normalizeDateRange,
  resourceTypeLabel,
  resultMap,
  useStore,
  type AuditLogData,
} from '@/views/logs/useStore'
import { useController } from '@/views/logs/useController'

const store = useStore()

function makeLog(overrides: Partial<AuditLogData> = {}): AuditLogData {
  return {
    id: 1,
    actor_id: 2,
    actor_name: 'user',
    action: 'create',
    result: 'success',
    resource_type: 'api_key',
    resource_id: '9',
    detail: { params: { name: 'k', token: '***' } },
    ip_address: '127.0.0.1',
    created_at: '2026-09-01T08:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenantAuditLogs).mockResolvedValue({
    items: [
      makeLog(),
      makeLog({ id: 2, action: 'login', resource_type: null, resource_id: null }),
    ],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  })
})

// ---------- store(只读流水,无写操作) ----------

describe('logs useStore(本租户作用域,真实接口对接)', () => {
  it('request 拼装分页与筛选参数(无 tenant_id),返回 items/total', async () => {
    const res = await store.request({
      page: 1,
      pageSize: 20,
      action: 'create',
      resource_id: ' 9 ',
      dateRange: [1000, 2000],
    })

    expect(listTenantAuditLogs).toHaveBeenCalledTimes(1)
    expect(listTenantAuditLogs).toHaveBeenCalledWith(
      {
        page: 1,
        page_size: 20,
        action: 'create',
        resource_id: '9',
        since_ms: 1000,
        until_ms: 2000,
      },
      undefined,
    )
    expect(res.total).toBe(2)
    expect(res.data[0]).toMatchObject({ id: 1, action: 'create' })
  })

  it('空筛选不传对应参数;signal 经 context 透传', async () => {
    const controller = new AbortController()
    await store.request(
      {
        page: 1,
        pageSize: 20,
        action: '',
        resource_id: '',
        dateRange: null,
      },
      { signal: controller.signal },
    )

    expect(listTenantAuditLogs).toHaveBeenCalledWith(
      {
        page: 1,
        page_size: 20,
        action: undefined,
        resource_id: undefined,
        since_ms: undefined,
        until_ms: undefined,
      },
      controller.signal,
    )
  })

  it('接口异常向上抛出(由表格层统一处理)', async () => {
    vi.mocked(listTenantAuditLogs).mockRejectedValue(new Error('network'))
    await expect(
      store.request({
        page: 1,
        pageSize: 20,
        action: '',
        resource_id: '',
        dateRange: null,
      }),
    ).rejects.toThrow('network')
  })
})

describe('筛选选项与映射常量', () => {
  it('操作筛选选项首项为「全部」,其余与映射词表一致', () => {
    expect(actionFilterOptions[0]).toEqual({ label: '全部操作', value: '' })
    expect(actionFilterOptions.slice(1).map((item) => item.value).sort()).toEqual(
      Object.keys(actionMap).sort(),
    )
  })

  it('结果/操作映射与时间格式化', () => {
    expect(resultMap.failed).toEqual({ label: '失败', type: 'error' })
    expect(actionMap.login.label).toBe('登录')
    expect(resourceTypeLabel('user')).toBe('用户')
    expect(formatDateTime(null)).toBe('—')
  })

  it('normalizeDateRange:null 透传', () => {
    expect(normalizeDateRange(null)).toBeNull()
  })

  it('normalizeDateRange:同一天归一化为 [当日0点, 当日23:59:59.999]', () => {
    const dayStart = new Date(2026, 8, 1).getTime()
    expect(normalizeDateRange([dayStart, dayStart])).toEqual([
      dayStart,
      new Date(2026, 8, 1, 23, 59, 59, 999).getTime(),
    ])
  })

  it('normalizeDateRange:跨天时归一化到 [起始日0点, 结束日23:59:59.999]', () => {
    const start = new Date(2026, 8, 1, 15, 30).getTime()
    const end = new Date(2026, 8, 5).getTime()
    expect(normalizeDateRange([start, end])).toEqual([
      new Date(2026, 8, 1).getTime(),
      new Date(2026, 8, 5, 23, 59, 59, 999).getTime(),
    ])
  })
})

// ---------- controller(挂载,真实表格驱动) ----------

describe('logs useController', () => {
  /** 挂载并渲染真实表格(remote 请求经 mock 立即返回) */
  async function mountWithTable() {
    let controller!: ReturnType<typeof useController>
    const wrapper = mount({
      setup: () => {
        controller = useController()
        return () => h('div', [h(controller.LmTable)])
      },
    }, { attachTo: document.body })
    await vi.waitFor(
      () => expect(document.body.querySelectorAll('tbody tr').length).toBeGreaterThan(0),
      { timeout: 3000 },
    )
    return { controller, wrapper }
  }

  it('点「详情」打开抽屉并锁定当前日志', async () => {
    const { controller, wrapper } = await mountWithTable()
    expect(controller.detailShow.value).toBe(false)
    expect(controller.activeLog.value).toBeNull()

    const detailBtn = [...document.body.querySelectorAll<HTMLButtonElement>('tbody button')].find(
      (btn) => btn.textContent?.includes('详情'),
    )
    expect(detailBtn).toBeTruthy()
    detailBtn!.click()
    await nextTick()

    expect(controller.detailShow.value).toBe(true)
    expect(controller.activeLog.value).toMatchObject({ actor_name: 'user' })
    wrapper.unmount()
  })

  it('资源列:有 resource_type 渲染标签,无则不渲染标签仅显示占位', async () => {
    const { controller, wrapper } = await mountWithTable()
    const rows = [...document.body.querySelectorAll('tbody tr')]
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('API Key')
    // 第二行 resource_type/resource_id 均为 null:不出现 '—' 占位标签,仅显示 '--'
    expect(rows[1].textContent).not.toContain('—')
    expect(rows[1].textContent).toContain('--')
    wrapper.unmount()
  })

  it('onActionChange 下发筛选参数并重取(请求参数含目标筛选)', async () => {
    const { controller, wrapper } = await mountWithTable()
    controller.onActionChange('login')
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listTenantAuditLogs).mock.calls.at(-1)?.[0]
      expect(lastCall?.action).toBe('login')
    })
    wrapper.unmount()
  })

  it('onDateChange 归一化整天区间后下发筛选并重取', async () => {
    const { controller, wrapper } = await mountWithTable()
    const dayStart = new Date(2026, 8, 1).getTime()
    controller.onDateChange([dayStart, dayStart])
    expect(controller.param.dateRange).toEqual([
      dayStart,
      new Date(2026, 8, 1, 23, 59, 59, 999).getTime(),
    ])
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listTenantAuditLogs).mock.calls.at(-1)?.[0]
      expect(lastCall).toMatchObject({
        since_ms: dayStart,
        until_ms: new Date(2026, 8, 1, 23, 59, 59, 999).getTime(),
      })
    })
    wrapper.unmount()
  })

  it('resetFilters 重置表格查询参数', async () => {
    const { controller, wrapper } = await mountWithTable()
    controller.onActionChange('login')
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listTenantAuditLogs).mock.calls.at(-1)?.[0]
      expect(lastCall?.action).toBe('login')
    })
    controller.resetFilters()
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listTenantAuditLogs).mock.calls.at(-1)?.[0]
      expect(lastCall?.action).toBeUndefined()
    })
    wrapper.unmount()
  })
})
