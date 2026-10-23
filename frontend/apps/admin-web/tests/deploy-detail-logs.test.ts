// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/api/deployment', () => ({
  listDeploymentLogs: vi.fn(),
}))
vi.mock('@/api/tail-log', () => ({
  tailContainerLog: vi.fn(),
}))
// n-virtual-list 依赖 ResizeObserver 测量视口(happy-dom 下高度为 0 → 视口窗口恒空),
// 以全量渲染插槽的桩替代(对齐 VueFlow 桩惯例):文本断言照常,不测虚拟窗口本身
vi.mock('naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('naive-ui')>()
  return {
    ...actual,
    NVirtualList: {
      name: 'NVirtualList',
      props: ['items', 'itemSize', 'itemResizable', 'keyField', 'paddingTop', 'paddingBottom'],
      template:
        '<div><template v-for="(item, index) in items" :key="item[keyField]"><slot :item="item" :index="index" /></template></div>',
    },
  }
})
vi.mock('@/views/deploy/components/deploy-list/useStore', async (importOriginal) => {
  const actual = await importOriginal<
    typeof import('@/views/deploy/components/deploy-list/useStore')
  >()
  return actual
})

import { listDeploymentLogs, type DeployLogItem } from '@/api/deployment'
import { tailContainerLog } from '@/api/tail-log'
import type { DeploymentInstance } from '@/api/deployment'
import ContainerConsole from '@/views/deploy/components/deploy-list/deploy-detail/logs/ContainerConsole.vue'
import LogTimeline from '@/views/deploy/components/deploy-list/deploy-detail/logs/LogTimeline.vue'
import {
  buildDisplayRows,
  buildTimelineRows,
  containerNameFor,
  firstLoadPage,
  formatAbsoluteTime,
  formatRelativeTime,
  isSameSecond,
  mergeConsoleLines,
  mergeLogsById,
  nodeDisplayName,
  stepTitle,
} from '@/views/deploy/components/deploy-list/deploy-detail/logs/logUtils'

function makeLog(overrides: Partial<DeployLogItem> = {}): DeployLogItem {
  return {
    id: 1,
    deployment_id: 'd1',
    node_id: 'node-01',
    task_id: null,
    step: 'pull_image',
    level: 'info',
    status: 'running',
    message: '镜像拉取中',
    extra: null,
    created_at: '2025-06-01T08:30:00Z',
    ...overrides,
  }
}

function makeInstance(overrides: Partial<DeploymentInstance> = {}): DeploymentInstance {
  return {
    instance_id: 'i0',
    deployment_id: 'd1',
    role: 'full',
    rank: 0,
    assigned_node: 'node-01',
    status: 'READY',
    container_id: 'c1',
    endpoint: null,
    last_error: null,
    started_at: null,
    ready_at: null,
    stopped_at: null,
    ...overrides,
  } as DeploymentInstance
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('logUtils 纯函数', () => {
  it('mergeLogsById:按 id 去重追加(since 含等值带回本地最后一条);无新行返回原数组', () => {
    const a = makeLog({ id: 1 })
    const b = makeLog({ id: 2, message: '第二条' })
    const merged = mergeLogsById([a], [a, b])
    expect(merged.map((item) => item.id)).toEqual([1, 2])

    // 全重复:返回原引用
    const again = mergeLogsById(merged, [a, b])
    expect(again).toBe(merged)
  })

  it('mergeLogsById:maxLogs 超限截尾保最新;未超限返回原引用;全重复超限也截', () => {
    const range = (start: number, end: number) =>
      Array.from({ length: end - start + 1 }, (_, index) => makeLog({ id: start + index }))

    // 未超限且无新行:返回原引用
    const two = range(1, 2)
    expect(mergeLogsById(two, [makeLog({ id: 2 })], 5)).toBe(two)

    // 新增后超限:截掉最旧,保留最新 maxLogs 条
    const merged = mergeLogsById(range(1, 4), [makeLog({ id: 5 })], 4)
    expect(merged.map((item) => item.id)).toEqual([2, 3, 4, 5])

    // 全重复但 existing 已超限:同样截尾
    expect(mergeLogsById(range(1, 5), [makeLog({ id: 5 })], 3).map((item) => item.id)).toEqual([
      3, 4, 5,
    ])
  })

  it('firstLoadPage:不超一页停在第 1 页;超页跳末页', () => {
    expect(firstLoadPage(500, 500)).toBe(1)
    expect(firstLoadPage(1200, 500)).toBe(3)
    expect(firstLoadPage(501, 500)).toBe(2)
  })

  it('containerNameFor:llmpod-<部署 ID 前 8 位>-<实例 ID>', () => {
    expect(containerNameFor('a1b2c3d4-9999', 'inst-xxxx')).toBe('llmpod-a1b2c3d4-inst-xxxx')
  })

  it('mergeConsoleLines:更早历史前插 + 跟随新行追加;全重复返回 null', () => {
    // 重放窗口 [x,y,a,b,c] + 跟随新行 d
    expect(mergeConsoleLines(['a', 'b', 'c'], ['x', 'y', 'a', 'b', 'c', 'd'])).toEqual([
      'x',
      'y',
      'a',
      'b',
      'c',
      'd',
    ])
    // 同窗口重连只带来新行
    expect(mergeConsoleLines(['a', 'b'], ['a', 'b', 'e'])).toEqual(['a', 'b', 'e'])
    // 全重复
    expect(mergeConsoleLines(['a'], ['a'])).toBeNull()
    // 全新(existing 空)
    expect(mergeConsoleLines([], ['a', 'b'])).toEqual(['a', 'b'])
  })

  it('stepTitle:文档 §4 词表映射;start 按 extra.action 细化;未知原样', () => {
    expect(stepTitle('queued', null)).toBe('排队中')
    expect(stepTitle('head_health_check', null)).toBe('主节点健康检查')
    expect(stepTitle('worker_health_check', null)).toBe('工作节点健康检查')
    expect(stepTitle('finalize', null)).toBe('生成端点')
    expect(stepTitle('running', null)).toBe('就绪')
    expect(stepTitle('start', { action: 'start' })).toBe('启动')
    expect(stepTitle('start', { action: 'restart' })).toBe('重启')
    expect(stepTitle('start', null)).toBe('启动')
    expect(stepTitle('stop', null)).toBe('停止')
    expect(stepTitle('wait_ready', null)).toBe('等待就绪')
    expect(stepTitle('unknown-step', null)).toBe('unknown-step')
  })

  it('nodeDisplayName:hostname > ip > node_id 短 hash;编排事件(无 node)为 null', () => {
    expect(
      nodeDisplayName({
        node_id: 'node-abcdefgh',
        extra: { node_hostname: 'host-01', node_ip: '10.0.1.1' },
      }),
    ).toBe('host-01')
    expect(nodeDisplayName({ node_id: 'node-abcdefgh', extra: { node_ip: '10.0.1.1' } })).toBe(
      '10.0.1.1',
    )
    expect(nodeDisplayName({ node_id: 'node-abcdefgh', extra: null })).toBe('node-abc')
    expect(nodeDisplayName({ node_id: null, extra: null })).toBeNull()
    expect(nodeDisplayName({ node_id: null, extra: { node_hostname: 'host-01' } })).toBe(
      'host-01',
    )
  })

  it('formatRelativeTime:相对时间区间边界;未来按刚刚;非法原样', () => {
    const now = new Date('2026-08-20T12:00:00').getTime()
    // 刚刚:diff < 5s
    expect(formatRelativeTime(new Date(now - 4000).toISOString(), now)).toBe('刚刚')
    // 30 秒前
    expect(formatRelativeTime(new Date(now - 30000).toISOString(), now)).toBe('30 秒前')
    // 15 分钟前
    expect(formatRelativeTime(new Date(now - 15 * 60000).toISOString(), now)).toBe('15 分钟前')
    // 3 小时前
    expect(formatRelativeTime(new Date(now - 3 * 3600000).toISOString(), now)).toBe('3 小时前')
    // 昨天(自然日)
    expect(formatRelativeTime('2026-08-19T08:30:00', now)).toBe('昨天 08:30')
    // 更早
    expect(formatRelativeTime('2026-08-10T09:05:00', now)).toBe('08-10 09:05')
    // 未来按刚刚
    expect(formatRelativeTime(new Date(now + 10000).toISOString(), now)).toBe('刚刚')
    // 非法原样
    expect(formatRelativeTime('not-a-date', now)).toBe('not-a-date')
  })

  it('formatRelativeTime:阈值分界(秒/分/时/天)与前天自然日', () => {
    const now = new Date('2026-08-20T12:00:00').getTime()
    const at = (diff: number) => new Date(now - diff).toISOString()
    // 秒分界:diff=4999 仍「刚刚」,5000 起进入「N 秒前」
    expect(formatRelativeTime(at(4999), now)).toBe('刚刚')
    expect(formatRelativeTime(at(5000), now)).toBe('5 秒前')
    // 分分界:59999 仍 59 秒前,60000 起 1 分钟前
    expect(formatRelativeTime(at(59_999), now)).toBe('59 秒前')
    expect(formatRelativeTime(at(60_000), now)).toBe('1 分钟前')
    // 时分界:3599999 仍 59 分钟前,3600000 起 1 小时前
    expect(formatRelativeTime(at(3_599_999), now)).toBe('59 分钟前')
    expect(formatRelativeTime(at(3_600_000), now)).toBe('1 小时前')
    // 天分界:86399999 仍 23 小时前;86400000 整 24h 进入「昨天/更早」分支(不再出现 N 小时前)
    expect(formatRelativeTime(at(86_399_999), now)).toBe('23 小时前')
    expect(formatRelativeTime(at(86_400_000), now)).not.toBe('24 小时前')
    // 24-48h 但属前天自然日:应走「更早」分支返回 MM-DD HH:mm,而非「昨天」
    expect(formatRelativeTime('2026-08-18T23:00:00', now)).toBe('08-18 23:00')
  })

  it('formatAbsoluteTime:YYYY-MM-DD HH:mm:ss;非法原样', () => {
    expect(formatAbsoluteTime('2026-08-20T12:34:56')).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/,
    )
    // 精确值:月/日补零(本地 getter 拼期望,参照 formatAbsoluteTime)
    const d = new Date('2026-01-05T03:04:05')
    const pad = (n: number) => String(n).padStart(2, '0')
    const expectText = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    expect(formatAbsoluteTime('2026-01-05T03:04:05')).toBe(expectText)
    expect(formatAbsoluteTime('not-a-date')).toBe('not-a-date')
  })

  it('isSameSecond:同秒 true、跨秒 false、非法 false', () => {
    expect(isSameSecond('2026-08-20T12:00:00.100Z', '2026-08-20T12:00:00.900Z')).toBe(true)
    expect(isSameSecond('2026-08-20T12:00:00Z', '2026-08-20T12:00:01Z')).toBe(false)
    expect(isSameSecond('not-a-date', '2026-08-20T12:00:00Z')).toBe(false)
    expect(isSameSecond('2026-08-20T12:00:00Z', 'not-a-date')).toBe(false)
  })

  it('buildTimelineRows:相邻 >10 分钟插分组;空数组/单条不插', () => {
    // 相邻 9 分钟不插 group
    const close = buildTimelineRows([
      makeLog({ id: 1, created_at: '2026-08-20T12:00:00Z' }),
      makeLog({ id: 2, created_at: '2026-08-20T12:09:00Z' }),
    ])
    expect(close).toHaveLength(2)
    expect(close.every((row) => row.kind === 'log')).toBe(true)

    // 恰好 10 分钟(等值)不插 group:严格大于才分组
    const exact = buildTimelineRows([
      makeLog({ id: 1, created_at: '2026-08-20T12:00:00Z' }),
      makeLog({ id: 2, created_at: '2026-08-20T12:10:00Z' }),
    ])
    expect(exact).toHaveLength(2)
    expect(exact.every((row) => row.kind === 'log')).toBe(true)

    // 11 分钟插入 group 且 gapText === '11 分钟后'
    const gapped = buildTimelineRows([
      makeLog({ id: 1, created_at: '2026-08-20T12:00:00Z' }),
      makeLog({ id: 2, created_at: '2026-08-20T12:11:00Z' }),
    ])
    expect(gapped).toHaveLength(3)
    const groupRow = gapped[1]
    expect(groupRow?.kind).toBe('group')
    if (groupRow?.kind === 'group') {
      expect(groupRow.gapText).toBe('11 分钟后')
    }

    // 同秒爆发 8 条 + 20 分钟后 1 条只插一条 group
    const burst = [
      ...Array.from({ length: 8 }, (_, index) =>
        makeLog({ id: index + 1, created_at: '2026-08-20T12:00:00Z' }),
      ),
      makeLog({ id: 9, created_at: '2026-08-20T12:20:00Z' }),
    ]
    expect(buildTimelineRows(burst).filter((row) => row.kind === 'group')).toHaveLength(1)

    // 空数组/单条不插
    expect(buildTimelineRows([])).toHaveLength(0)
    expect(buildTimelineRows([makeLog()])).toHaveLength(1)
  })

  it('buildTimelineRows:跨天 group 的 label 为 MM-DD HH:mm', () => {
    const rows = buildTimelineRows([
      makeLog({ id: 1, created_at: '2026-08-20T23:00:00' }),
      makeLog({ id: 2, created_at: '2026-08-21T00:00:00' }),
    ])
    expect(rows).toHaveLength(3)
    const group = rows[1]
    expect(group?.kind).toBe('group')
    if (group?.kind === 'group') {
      expect(group.label).toBe('08-21 00:00')
    }
  })

  it('buildTimelineRows:gapText 整小时不带分钟/非整小时带分钟', () => {
    // <24h 整小时
    const whole = buildTimelineRows([
      makeLog({ id: 1, created_at: '2026-08-20T12:00:00' }),
      makeLog({ id: 2, created_at: '2026-08-20T14:00:00' }),
    ])
    const wholeGroup = whole[1]
    expect(wholeGroup?.kind).toBe('group')
    if (wholeGroup?.kind === 'group') {
      expect(wholeGroup.gapText).toBe('2 小时后')
    }

    // <24h 非整小时
    const partial = buildTimelineRows([
      makeLog({ id: 1, created_at: '2026-08-20T12:00:00' }),
      makeLog({ id: 2, created_at: '2026-08-20T14:15:00' }),
    ])
    const partialGroup = partial[1]
    expect(partialGroup?.kind).toBe('group')
    if (partialGroup?.kind === 'group') {
      expect(partialGroup.gapText).toBe('2 小时 15 分钟后')
    }
  })

  it('buildDisplayRows:同秒折叠 ·、group 行保留、行内文本全部预计算', () => {
    const now = new Date('2026-08-20T12:30:00').getTime()
    const at = (diff: number) => new Date(now - diff).toISOString()
    const firstIso = at(1_230_000)
    const rows = buildDisplayRows(
      [
        makeLog({
          id: 1,
          created_at: firstIso,
          step: 'pull_image',
          extra: { node_hostname: 'host-01' },
        }),
        // 与前一条同秒(仅晚 10ms),时间列折叠
        makeLog({ id: 2, created_at: at(1_229_990), message: '同秒' }),
        // 与前一条恰隔 20 分钟,插分组行
        makeLog({ id: 3, created_at: at(29_990) }),
      ],
      now,
    )
    expect(rows).toHaveLength(4)

    // 第一条 log:相对时间预计算 + 绝对时间 title + tag/节点文本
    const first = rows[0]
    expect(first?.kind).toBe('log')
    if (first?.kind === 'log') {
      expect(first.key).toBe(1)
      expect(first.timeText).toBe('20 分钟前')
      expect(first.timeTitle).toBe(formatAbsoluteTime(firstIso))
      expect(first.tagText).toBe(stepTitle('pull_image', first.item.extra))
      expect(first.nodeText).toBe('host-01')
    }

    // 同秒第二条折叠为 ·
    expect(rows[1]?.kind === 'log' && rows[1].timeText).toBe('·')

    // 分组行
    const group = rows[2]
    expect(group?.kind).toBe('group')
    if (group?.kind === 'group') {
      expect(group.key).toContain('group')
      expect(group.gapText).toBe('20 分钟后')
    }

    // 分组后的新 log:与前一条 log 不同秒,不折叠
    expect(rows[3]?.kind === 'log' && rows[3].timeText).not.toBe('·')
  })
})

describe('LogTimeline 时间线', () => {
  function mockPage(items: DeployLogItem[], total = items.length, totalPages = 1) {
    return { items, total, page: 1, page_size: 500, total_pages: totalPages }
  }

  it('首屏 page=1 探 total;超一页跳末页拿最新一段', async () => {
    const probe = mockPage([], 1200, 3)
    const lastPage = mockPage(
      [makeLog({ id: 1198 }), makeLog({ id: 1199 }), makeLog({ id: 1200 })],
      1200,
      3,
    )
    vi.mocked(listDeploymentLogs)
      .mockResolvedValueOnce(probe)
      .mockResolvedValueOnce(lastPage)
    const wrapper = mount(LogTimeline, {
      props: { deploymentId: 'd1', status: 'stopped' },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(document.body.textContent).toContain('镜像拉取中'))
    expect(listDeploymentLogs).toHaveBeenCalledTimes(2)
    expect(vi.mocked(listDeploymentLogs).mock.calls[0]![1]).toMatchObject({ page: 1, page_size: 500 })
    expect(vi.mocked(listDeploymentLogs).mock.calls[1]![1]).toMatchObject({ page: 3, page_size: 500 })
    wrapper.unmount()
  })

  it('creating 状态 5s 增量:since=本地最后一条 created_at;终态不轮询', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(listDeploymentLogs).mockResolvedValue(
        mockPage([makeLog({ id: 1, created_at: '2025-06-01T08:30:00Z' })]),
      )
      const wrapper = mount(LogTimeline, {
        props: { deploymentId: 'd1', status: 'creating' },
        attachTo: document.body,
      })
      await vi.advanceTimersByTimeAsync(0)
      expect(listDeploymentLogs).toHaveBeenCalledTimes(1)

      // creating:5s 后增量,带 since
      await vi.advanceTimersByTimeAsync(5000)
      expect(listDeploymentLogs).toHaveBeenCalledTimes(2)
      expect(vi.mocked(listDeploymentLogs).mock.calls[1]![1]).toMatchObject({
        since: '2025-06-01T08:30:00Z',
        page_size: 500,
      })

      // 终态:轮询停止
      await wrapper.setProps({ status: 'stopped' })
      await vi.advanceTimersByTimeAsync(20000)
      expect(listDeploymentLogs).toHaveBeenCalledTimes(2)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('nodeId 入口(物理节点卡片):首屏与增量均带 node_id 过滤', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(listDeploymentLogs).mockResolvedValue(
        mockPage([makeLog({ id: 1, created_at: '2025-06-01T08:30:00Z' })]),
      )
      const wrapper = mount(LogTimeline, {
        props: { deploymentId: 'd1', status: 'creating', nodeId: 'node-01' },
        attachTo: document.body,
      })
      await vi.advanceTimersByTimeAsync(0)
      expect(vi.mocked(listDeploymentLogs).mock.calls[0]![1]).toMatchObject({
        node_id: 'node-01',
      })
      await vi.advanceTimersByTimeAsync(5000)
      expect(vi.mocked(listDeploymentLogs).mock.calls[1]![1]).toMatchObject({
        node_id: 'node-01',
        since: '2025-06-01T08:30:00Z',
      })
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('时间列:同秒合并为 ·;跨 10 分钟插入分组行;悬浮 title 给完整时间', async () => {
    vi.mocked(listDeploymentLogs).mockResolvedValue(
      mockPage([
        makeLog({ id: 1, created_at: '2026-08-20T10:00:01.100Z', message: '第一条' }),
        makeLog({ id: 2, created_at: '2026-08-20T10:00:01.900Z', message: '同秒第二条' }),
        makeLog({ id: 3, created_at: '2026-08-20T10:20:05.000Z', message: '二十分钟后' }),
      ]),
    )
    const wrapper = mount(LogTimeline, {
      props: { deploymentId: 'd1', status: 'stopped' },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(document.body.textContent).toContain('二十分钟后'))

    // 分组行:含间隔文案
    expect(document.body.textContent).toContain('20 分钟后')

    // 时间列:第一行是相对时间文本,第二行同秒合并
    const timeSpans = [...document.body.querySelectorAll('span[title]')].filter((el) =>
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test((el as HTMLElement).title ?? ''),
    )
    const texts = timeSpans.map((el) => (el.textContent ?? '').trim())
    expect(timeSpans).toHaveLength(3)
    expect(texts[0]).not.toBe('·')
    expect(texts[1]).toBe('·')
    expect(texts[2]).not.toBe('·')

    wrapper.unmount()
  })
})

describe('ContainerConsole 容器 stdout', () => {
  /** 捕获开流参数;返回手动推帧的句柄(模拟常连接) */
  function captureStream() {
    let captured: {
      nodeId: string
      containerName: string
      lines: number
      signal: AbortSignal
      onFrame: (frame: { event: string; data?: string; message?: string }) => void
    } | null = null
    vi.mocked(tailContainerLog).mockImplementation(async (options) => {
      captured = options as never
      return new Promise<void>(() => {})
    })
    return () => captured
  }

  it('挂载自动开流:容器名拼接 + 默认 200 行;data 帧行渲染', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: {
        deploymentId: 'a1b2c3d4-9999',
        instances: [makeInstance(), makeInstance({ instance_id: 'i1', rank: 1 })],
      },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    const stream = getStream()!
    expect(stream.nodeId).toBe('node-01')
    expect(stream.containerName).toBe('llmpod-a1b2c3d4-i0')
    expect(stream.lines).toBe(200)

    stream.onFrame({ event: 'data', data: 'INFO engine started' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('INFO engine started'))
    wrapper.unmount()
  })

  it('initialInstanceId 定位:弹窗打开即追踪指定实例(而非首个)', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: {
        deploymentId: 'd1',
        instances: [makeInstance(), makeInstance({ instance_id: 'i1', rank: 1 })],
        initialInstanceId: 'i1',
      },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    expect(getStream()!.containerName).toBe('llmpod-d1-i1')
    wrapper.unmount()
  })

  it('待调度实例不可选;卸载必须 abort(全局单流)', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: {
        deploymentId: 'd1',
        instances: [
          makeInstance({ instance_id: 'i9', rank: 9, assigned_node: null }),
          makeInstance(),
        ],
      },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    // 跳过无节点实例,选了 i0
    expect(getStream()!.containerName).toBe('llmpod-d1-i0')

    const signal = getStream()!.signal
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
  })

  it('end 帧置「已结束」;重连按钮重开流', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: { deploymentId: 'd1', instances: [makeInstance()] },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    getStream()!.onFrame({ event: 'end' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('流已结束'))

    const reconnect = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('重连'),
    )!
    reconnect.click()
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(2))
    wrapper.unmount()
  })

  it('error 帧:状态与错误信息展示', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: { deploymentId: 'd1', instances: [makeInstance()] },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    getStream()!.onFrame({ event: 'error', message: '容器不存在' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('容器不存在'))
    wrapper.unmount()
  })

  it('虚拟列表 padding 属性必须传数字:rem 字符串毒化滚动窗口计算导致弹窗空白', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: { deploymentId: 'd1', instances: [makeInstance()] },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    getStream()!.onFrame({ event: 'data', data: 'line-1' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('line-1'))

    const virtualList = wrapper.findComponent({ name: 'NVirtualList' })
    expect(virtualList.exists()).toBe(true)
    // vueuc startIndex = getBound(scrollTop - depx(paddingTop)):seemly depx() 只认数字/px,
    // rem 字符串得 NaN → getBound(NaN) 恒返回 N/2(二分首次探测命中 else 分支),
    // 渲染窗口钉死列表中部与真实滚动脱钩 → 行数超过一屏后弹窗恒空白(回归防线:必须数字)
    expect(Number.isFinite(virtualList.props('paddingTop') as number)).toBe(true)
    expect(Number.isFinite(virtualList.props('paddingBottom') as number)).toBe(true)
    wrapper.unmount()
  })

  it('程序性 scroll 事件不解除跟随;用户上滚才解除并挂角标,贴底自动恢复', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: { deploymentId: 'd1', instances: [makeInstance()] },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    const stream = getStream()!
    stream.onFrame({ event: 'data', data: 'line-1' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('line-1'))

    const virtualList = wrapper.findComponent({ name: 'NVirtualList' })
    // 程序滚动/行高测量锚定也会派发 scroll:距底很远(e.g. 5000-100-400)不应解除跟随
    Object.defineProperty(virtualList.element, 'scrollHeight', { value: 5000, configurable: true })
    Object.defineProperty(virtualList.element, 'scrollTop', { value: 100, configurable: true })
    Object.defineProperty(virtualList.element, 'clientHeight', { value: 400, configurable: true })
    await virtualList.trigger('scroll')
    stream.onFrame({ event: 'data', data: 'line-2' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('line-2'))
    expect(document.body.textContent).not.toContain('条新日志')

    // 用户上滚(deltaY<0)解除跟随:后续新行只计角标不自动滚
    await virtualList.trigger('wheel', { deltaY: -120 })
    stream.onFrame({ event: 'data', data: 'line-3' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('1 条新日志'))

    // 用户滚回底部:scroll 距底 <30px(happy-dom 下 0-0-0)恢复跟随并清角标
    Object.defineProperty(virtualList.element, 'scrollHeight', { value: 0, configurable: true })
    Object.defineProperty(virtualList.element, 'scrollTop', { value: 0, configurable: true })
    await virtualList.trigger('scroll')
    stream.onFrame({ event: 'data', data: 'line-4' })
    await vi.waitFor(() => expect(document.body.textContent).toContain('line-4'))
    expect(document.body.textContent).not.toContain('条新日志')
    wrapper.unmount()
  })

  it('跟随超限截头:行数封顶 4000,丢弃最旧、保留最新一段', async () => {
    const getStream = captureStream()
    const wrapper = mount(ContainerConsole, {
      props: { deploymentId: 'd1', instances: [makeInstance()] },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(tailContainerLog).toHaveBeenCalledTimes(1))
    const stream = getStream()!
    const total = 4000 + 5
    for (let index = 1; index <= total; index += 1) {
      stream.onFrame({ event: 'data', data: `line-${String(index).padStart(4, '0')}` })
    }
    await vi.waitFor(() => expect(document.body.textContent).toContain('line-4005'))

    // 封顶 4000 行,最旧 5 行被丢弃,窗口起点变为 line-0006
    expect(document.body.textContent).toContain('已收 4000 行')
    expect(document.body.textContent).not.toContain('line-0005')
    expect(document.body.textContent).toContain('line-0006')
    wrapper.unmount()
  })
})
