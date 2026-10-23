// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/deployment', async (importOriginal) => ({
  // normalizedTopology 是纯函数,直接用真实实现
  ...(await importOriginal<typeof import('@/api/deployment')>()),
  createDeployment: vi.fn(),
  deleteDeployment: vi.fn(),
  getDeployment: vi.fn(),
  listDeployments: vi.fn(),
  startDeployment: vi.fn(),
  stopDeployment: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: vi.fn(),
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import {
  deleteDeployment,
  getDeployment,
  startDeployment,
  stopDeployment,
} from '@/api/deployment'
import { useConfirm, message } from '@repo/hooks'
import type { DeploymentDetailOut, ModelDeploymentOut } from '@/api/deployment'
import DeployDetailDrawer from '@/views/deploy/components/deploy-list/DeployDetailDrawer.vue'

function makeRecord(overrides: Partial<ModelDeploymentOut> = {}): ModelDeploymentOut {
  return {
    deployment_id: 'd1',
    cluster_id: 1,
    name: 'prod-deploy',
    model_id: 7,
    engine: 'vllm',
    status: 'running',
    api_endpoint: null,
    last_error: null,
    phase: 'RUNNING',
    version: 1,
    spec: { topology: { tp: 2, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 } },
    created_at: '2025-06-01T08:30:00Z',
    updated_at: '2025-06-01T09:00:00Z',
    ...overrides,
  } as ModelDeploymentOut
}

function makeDetail(overrides: Partial<DeploymentDetailOut> = {}): DeploymentDetailOut {
  return {
    deployment: makeRecord(),
    instances: [
      {
        instance_id: 'i1',
        deployment_id: 'd1',
        role: 'full',
        rank: 0,
        assigned_node: 'node-01',
        status: 'READY',
        container_id: 'c1',
        endpoint: 'http://10.0.0.1:8000',
        last_error: null,
        started_at: null,
        ready_at: null,
        stopped_at: null,
      },
    ],
    endpoints: [
      {
        engine_type: 'vllm',
        protocol: 'openai',
        api_base: 'http://10.0.0.1:8000',
        path: '/v1/chat/completions',
        served_model: 'Qwen2.5-7B',
        context_window: 8192,
        operations: [],
        health_status: 'healthy',
      },
    ],
    ...overrides,
  } as DeploymentDetailOut
}

/** 挂载后打开抽屉(watch 无 immediate,需 show 由 false→true 触发加载) */
async function mountDrawer(record: ModelDeploymentOut | null, open = true) {
  const wrapper = mount(DeployDetailDrawer, {
    props: { show: false, record },
    attachTo: document.body,
  })
  if (open) {
    await wrapper.setProps({ show: true })
    await nextTick()
  }
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getDeployment).mockResolvedValue(makeDetail())
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('DeployDetailDrawer 详情加载与回显', () => {
  it('打开时按 deployment_id 拉详情,分区渲染端点/算力/拓扑', async () => {
    const wrapper = await mountDrawer(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalledWith('d1'))
    await nextTick()

    const text = document.body.textContent ?? ''
    expect(text).toContain('部署详情-prod-deploy')
    expect(text).toContain('http://10.0.0.1:8000')
    expect(text).toContain('/v1/chat/completions')
    expect(text).toContain('2 卡') // gpuSummary: tp2 × replicas1
    expect(text).toContain('tp=2 · pp=1 · 副本 1 · 节点 1 · 上下文 8192')
    expect(text).toContain('实例(1)')
    expect(text).toContain('node-01')
    wrapper.unmount()
  })

  it('详情拉取失败:回退列表行快照展示', async () => {
    vi.mocked(getDeployment).mockRejectedValue(new Error('boom'))
    const wrapper = await mountDrawer(makeRecord({ name: 'fallback-deploy' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()
    await nextTick()

    const text = document.body.textContent ?? ''
    expect(text).toContain('部署详情-fallback-deploy')
    // 端点分区回退文案
    expect(text).toContain('部署完成后生成')
    wrapper.unmount()
  })

  it('record 为 null 时打开也不拉详情', async () => {
    const wrapper = await mountDrawer(null, false)
    expect(getDeployment).not.toHaveBeenCalled()
    await wrapper.setProps({ show: true })
    await nextTick()
    expect(getDeployment).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('last_error 置顶告警展示', async () => {
    vi.mocked(getDeployment).mockResolvedValue(
      makeDetail({ deployment: makeRecord({ status: 'failed', last_error: '镜像拉取失败' }) }),
    )
    const wrapper = await mountDrawer(makeRecord({ status: 'failed', last_error: '镜像拉取失败' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()
    expect(document.body.textContent).toContain('镜像拉取失败')
    wrapper.unmount()
  })
})

describe('DeployDetailDrawer 停止/删除', () => {
  it('running 状态显示停止按钮;确认后调接口并重拉详情', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(stopDeployment).mockResolvedValue({ deployment_id: 'd1', status: 'stopped' })
    const wrapper = await mountDrawer(makeRecord({ status: 'running' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const stopBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('停止'),
    )
    expect(stopBtn).toBeTruthy()
    stopBtn!.click()
    await vi.waitFor(() => expect(stopDeployment).toHaveBeenCalledWith('d1'))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已停止:prod-deploy'))
    // 停止后重拉详情同步状态
    expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('stopped 状态隐藏停止按钮', async () => {
    vi.mocked(getDeployment).mockResolvedValue(makeDetail({ deployment: makeRecord({ status: 'stopped' }) }))
    const wrapper = await mountDrawer(makeRecord({ status: 'stopped' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const stopBtn = [...document.body.querySelectorAll('button')].find(
      (btn) => btn.textContent?.trim() === '停止',
    )
    expect(stopBtn).toBeUndefined()
    wrapper.unmount()
  })

  it('stopped 状态显示启动按钮;确认后调 start 并重拉详情', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(startDeployment).mockResolvedValue({})
    vi.mocked(getDeployment).mockResolvedValue(
      makeDetail({ deployment: makeRecord({ status: 'stopped' }) }),
    )
    const wrapper = await mountDrawer(makeRecord({ status: 'stopped' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const startBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('启动'),
    )
    expect(startBtn).toBeTruthy()
    startBtn!.click()
    await vi.waitFor(() => expect(startDeployment).toHaveBeenCalledWith('d1', false))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已启动:prod-deploy'))
    // 启动成功后重拉详情同步状态
    expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('running 状态隐藏启动按钮', async () => {
    const wrapper = await mountDrawer(makeRecord({ status: 'running' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const startBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.trim() === '启动',
    )
    expect(startBtn).toBeUndefined()
    wrapper.unmount()
  })

  it('删除:确认后调接口,提示并关闭抽屉;取消不调用', async () => {
    vi.mocked(deleteDeployment).mockResolvedValue({ deployment_id: 'd1', deleted: true })
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    const wrapper = await mountDrawer(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const removeBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('删除'),
    )!
    removeBtn.click()
    await vi.waitFor(() => expect(deleteDeployment).toHaveBeenCalledWith('d1'))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已删除:prod-deploy'))
    // 抽屉关闭:emit update:show false;并 emit removed(父级部署详情视图据此退出回列表)
    expect(wrapper.emitted('update:show')?.[0]).toEqual([false])
    expect(wrapper.emitted('removed')).toHaveLength(1)
    wrapper.unmount()

    // 取消:不调用删除接口
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))
    vi.mocked(deleteDeployment).mockClear()
    const wrapper2 = await mountDrawer(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()
    const removeBtn2 = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('删除'),
    )!
    removeBtn2.click()
    await nextTick()
    await nextTick()
    expect(deleteDeployment).not.toHaveBeenCalled()
    wrapper2.unmount()
  })
})
