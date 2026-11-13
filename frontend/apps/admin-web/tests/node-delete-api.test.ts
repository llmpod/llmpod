import { beforeEach, describe, expect, it, vi } from 'vitest'

// request 链路(http/主题/message)与接口契约无关,整体 mock 掉,只断言真正发出去的配置
vi.mock('@/api/request', () => ({ request: vi.fn() }))

import { deleteNode } from '@/api/node'
import { request } from '@/api/request'

const requestMock = vi.mocked(request)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('注销节点接口:force 只在强制移除时出现', () => {
  it('普通注销不带 force(与旧行为一致)', async () => {
    await deleteNode('n1')
    expect(requestMock).toHaveBeenCalledWith({
      url: '/admin/nodes/n1/delete',
      method: 'POST',
      params: undefined,
    })
  })

  it('force=false 等价于普通注销,不传参', async () => {
    await deleteNode('n1', { force: false })
    expect(requestMock.mock.calls[0][0].params).toBeUndefined()
  })

  it('强制移除带 force=true', async () => {
    await deleteNode('n1', { force: true })
    expect(requestMock.mock.calls[0][0]).toMatchObject({
      url: '/admin/nodes/n1/delete',
      method: 'POST',
      params: { force: true },
    })
  })
})
