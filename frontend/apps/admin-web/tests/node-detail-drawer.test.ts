// @vitest-environment happy-dom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VNode } from 'vue'

const { getDetailMock, removeMock, forceRemoveMock, confirmMock, messageMock } = vi.hoisted(() => ({
  getDetailMock: vi.fn(),
  removeMock: vi.fn(),
  forceRemoveMock: vi.fn(),
  /** 二次确认桩:默认通过;置为 reject 可模拟取消 */
  confirmMock: vi.fn(),
  messageMock: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: (...args: unknown[]) => confirmMock(...args),
    message: messageMock,
  }
})

// store 整体替换:抽屉只关心「详情 + 两个删除动作」,不拖起列表/GPU 请求
vi.mock('@/views/node-list/useStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/views/node-list/useStore')>()
  return {
    ...actual,
    useStore: () => ({
      getDetail: getDetailMock,
      toggleMaintenance: vi.fn(),
      upgrade: vi.fn(),
      remove: removeMock,
      forceRemove: forceRemoveMock,
    }),
  }
})

import type { NodeData } from '@/api/node'
import NodeDetailDrawer from '@/views/node-list/NodeDetailDrawer.vue'
import type { ComputeNode } from '@/views/node-list/useStore'

/** 二次确认入参(断言 checkbox 配置与文案用) */
interface ConfirmOptions {
  title?: string
  content?: string | VNode | (() => VNode)
  checkbox?: string | { label: string, defaultChecked?: boolean, required?: boolean }
}

function makeNode(overrides: Partial<NodeData> = {}): NodeData {
  return {
    id: 'n1',
    cluster_id: 1,
    hostname: 'gpu-01',
    ip: '10.0.0.1',
    remark: null,
    status: 'offline',
    last_heartbeat_at: '2026-11-01T10:00:00Z',
    cpu_cores: 16,
    cpu_arch: 'x86_64',
    memory_gb: 64,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-11-01T10:00:00Z',
    cluster_name: '默认集群',
    version: '26.11.12',
    version_status: 'ok',
    runtime: null,
    ...overrides,
  }
}

/** 打开抽屉(详情接口按传入状态返回),返回挂载实例供调用方断言后卸载 */
async function openDrawer(status: NodeData['status']) {
  const node = makeNode({ status })
  getDetailMock.mockResolvedValue(node)
  const wrapper = mount(NodeDetailDrawer, {
    props: { show: false, node: node as ComputeNode },
    attachTo: document.body,
  })
  await wrapper.setProps({ show: true })
  await flushPromises()
  return wrapper
}

/** 按文案取按钮(naive 按钮内层包 span,textContent 命中即可) */
function findButton(label: string): HTMLButtonElement | undefined {
  return Array.from(document.body.querySelectorAll('button')).find((btn) =>
    (btn.textContent ?? '').includes(label),
  )
}

/** 取一次 useConfirm 的入参 */
function confirmOptions(index = 0): ConfirmOptions {
  return confirmMock.mock.calls[index][0] as ConfirmOptions
}

/** 从 VNode 收集文本(确认弹窗文案是渲染函数) */
function collectText(node: unknown): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(collectText).join('')
  const vnode = node as { children?: unknown }
  return collectText(vnode.children)
}

/** 渲染确认弹窗的 content 并取文本 */
function contentText(options: ConfirmOptions): string {
  const content = options.content
  if (content == null) return ''
  return typeof content === 'function' ? collectText(content()) : collectText(content)
}

beforeEach(() => {
  vi.clearAllMocks()
  confirmMock.mockResolvedValue(undefined)
  removeMock.mockResolvedValue({
    id: 'n1',
    deleted: true,
    notified: false,
    cleaned: false,
    deleted_deployments: [],
  })
  forceRemoveMock.mockResolvedValue({
    id: 'n1',
    deleted: true,
    notified: false,
    cleaned: false,
    deleted_deployments: ['dep-a', 'dep-b'],
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('NodeDetailDrawer 注销节点', () => {
  it('强制移除不是独立按钮:节点操作里只有「注销节点」', async () => {
    const wrapper = await openDrawer('offline')
    expect(findButton('注销节点')).toBeTruthy()
    expect(findButton('强制移除')).toBeUndefined()
    wrapper.unmount()
  })

  it('离线节点:注销确认内置必勾的强制移除,并说明级联后果', async () => {
    const wrapper = await openDrawer('offline')
    findButton('注销节点')!.click()
    await flushPromises()

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(confirmOptions().title).toBe('注销节点')
    // 必勾:不勾选时确认按钮是禁用态,点不动注销
    expect(confirmOptions().checkbox).toMatchObject({ required: true })
    expect((confirmOptions().checkbox as { label: string }).label).toContain('强制移除')

    const text = contentText(confirmOptions())
    expect(text).toContain('已离线')
    expect(text).toContain('级联删除')
    expect(text).toContain('7 天注销标记')
    expect(text).toContain('需人工清理')
    expect(text).toContain('source=node')
    wrapper.unmount()
  })

  it('离线节点勾选确认:带 force 调接口,并提示连带删除的部署数与人工清理', async () => {
    const wrapper = await openDrawer('offline')
    confirmMock.mockResolvedValue(true)
    findButton('注销节点')!.click()
    await flushPromises()

    expect(forceRemoveMock).toHaveBeenCalledWith('n1')
    expect(removeMock).not.toHaveBeenCalled()
    expect(messageMock.success).toHaveBeenCalledWith('已强制移除:gpu-01,连带删除 2 个部署')
    expect(messageMock.warning).toHaveBeenCalledWith(
      '机器上的容器与数据卷需人工清理;source=node 的模型需到模型页删除',
    )
    wrapper.unmount()
  })

  it('在线节点:确认不带强制移除选项,走原来的注销', async () => {
    const wrapper = await openDrawer('online')
    findButton('注销节点')!.click()
    await flushPromises()

    expect(confirmOptions().checkbox).toBeUndefined()
    expect(contentText(confirmOptions())).toContain('确定注销节点「gpu-01」吗?')
    expect(removeMock).toHaveBeenCalledWith('n1')
    expect(forceRemoveMock).not.toHaveBeenCalled()
    expect(messageMock.success).toHaveBeenCalledWith('已注销:gpu-01')
    expect(messageMock.warning).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('确认未勾选(false)时按普通注销兜底', async () => {
    const wrapper = await openDrawer('offline')
    confirmMock.mockResolvedValue(false)
    findButton('注销节点')!.click()
    await flushPromises()

    expect(removeMock).toHaveBeenCalledWith('n1')
    expect(forceRemoveMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('取消二次确认不调接口', async () => {
    const wrapper = await openDrawer('offline')
    confirmMock.mockRejectedValue(new Error('cancel'))
    findButton('注销节点')!.click()
    await flushPromises()

    expect(removeMock).not.toHaveBeenCalled()
    expect(forceRemoveMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
