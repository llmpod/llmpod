// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/deployment', () => ({ scalePreview: vi.fn(), scaleDeployment: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useDialogClose: vi.fn(() => closeSpy),
    message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  }
})

import { scaleDeployment, scalePreview } from '@/api/deployment'
import { message } from '@repo/hooks'
import type { ScalePreviewOut } from '@/api/deployment'
import ScaleDialog from '@/views/deploy/components/deploy-list/deploy-detail/components/ScaleDialog.vue'

const closeSpy = vi.fn()

function makePreview(overrides: Partial<ScalePreviewOut> = {}): ScalePreviewOut {
  return {
    current_copies: 2,
    per_copy_gpus: 2,
    max_feasible_copies: 4,
    free_gpus: 6,
    suggested_placement: { gpu_uuids: ['g5', 'g6', 'g7', 'g8'], node_ids: ['n1', 'n2'] },
    ...overrides,
  }
}

const onScaled = vi.fn()

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(ScaleDialog, {
    attachTo: document.body,
    props: {
      deploymentId: 'd1',
      deploymentName: 'dep',
      currentCopies: 2,
      replicaSeqs: [1, 2],
      hostnameOf: { n1: 'gpu-01', n2: 'gpu-02' },
      onScaled,
      ...props,
    },
  })
}

/** 直接向份数输入框发 v-model 更新(绕开 happy-dom 下 native stepper 交互) */
async function setCopies(wrapper: ReturnType<typeof mountDialog>, value: number | null) {
  wrapper.getComponent({ name: 'InputNumber' }).vm.$emit('update:value', value)
  await nextTick()
}

function buttonByText(text: string): HTMLButtonElement | undefined {
  return [...document.body.querySelectorAll('button')].find((item) =>
    item.textContent?.includes(text),
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(scalePreview).mockResolvedValue(makePreview())
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ScaleDialog 预览加载', () => {
  it('挂载即拉预览:四格资源概览渲染,目标份数回填当前值,方向未变确认禁用', async () => {
    const wrapper = mountDialog()
    await vi.waitFor(() => expect(scalePreview).toHaveBeenCalledWith('d1'))

    const text = document.body.textContent ?? ''
    expect(text).toContain('当前份数')
    expect(text).toContain('2 份')
    expect(text).toContain('2 张/份')
    expect(text).toContain('6 张')
    expect(text).toContain('可扩上限')
    expect(text).toContain('4 份')
    // 初始 direction=null:提示目标与当前相同,确认按钮禁用
    expect(text).toContain('目标份数与当前相同')
    expect(buttonByText('确认调整')?.disabled).toBe(true)
    wrapper.unmount()
  })

  it('预览失败展示重试入口;重试成功后恢复资源概览', async () => {
    vi.mocked(scalePreview).mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountDialog()
    await vi.waitFor(() => expect(document.body.textContent).toContain('扩缩预览加载失败'))

    // 失败态不给确认入口,提供重试
    expect(buttonByText('确认调整')?.disabled).toBe(true)
    buttonByText('重试')!.click()
    await vi.waitFor(() => expect(scalePreview).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    expect(document.body.textContent).not.toContain('扩缩预览加载失败')
    wrapper.unmount()
  })
})

describe('ScaleDialog 方向说明', () => {
  it('扩容:份数上调展示新增份 × 单副本规格与建议落点(hostname 可读化),确认可用', async () => {
    const wrapper = mountDialog()
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    await setCopies(wrapper, 3)

    const text = document.body.textContent ?? ''
    expect(text).toContain('扩容至 3 份:新增 1 份 × 2 张')
    expect(text).toContain('建议落点:gpu-01、gpu-02')
    expect(buttonByText('确认调整')?.disabled).toBe(false)
    wrapper.unmount()
  })

  it('扩容差卡:落点为空时展示差卡提示,超过可扩上限确认保持禁用', async () => {
    vi.mocked(scalePreview).mockResolvedValue(
      makePreview({ max_feasible_copies: 2, free_gpus: 1, suggested_placement: null }),
    )
    const wrapper = mountDialog()
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    await setCopies(wrapper, 3)

    const text = document.body.textContent ?? ''
    expect(text).toContain('空闲卡不足(差 1 张),无法再扩')
    expect(text).toContain('空闲卡不足:再扩 1 份需 2 张,当前空闲 1 张')
    expect(buttonByText('确认调整')?.disabled).toBe(true)
    wrapper.unmount()
  })

  it('缩容:LIFO 从最新序号摘除并列出移除份与服务不断保证', async () => {
    // 弹窗挂载即拉预览,份数 mock 须在 mount 前就位
    vi.mocked(scalePreview).mockResolvedValue(makePreview({ current_copies: 3 }))
    const wrapper = mountDialog({ replicaSeqs: [1, 2, 3], currentCopies: 3 })
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    await setCopies(wrapper, 1)

    const text = document.body.textContent ?? ''
    expect(text).toContain('缩容至 1 份:按 LIFO 移除 副本3、副本2')
    expect(text).toContain('其余份服务不断')
    expect(buttonByText('确认调整')?.disabled).toBe(false)
    wrapper.unmount()
  })

  it('replicaSeqs 缺层(无 replicas 权威数据):按 1..N 推导移除序号', async () => {
    vi.mocked(scalePreview).mockResolvedValue(makePreview({ current_copies: 3 }))
    const wrapper = mountDialog({ replicaSeqs: [], currentCopies: 3 })
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    await setCopies(wrapper, 2)

    expect(document.body.textContent).toContain('按 LIFO 移除 副本3')
    wrapper.unmount()
  })
})

describe('ScaleDialog 提交流程', () => {
  it('确认:透传目标份数调 scale,成功后提示/回调父级刷新并关窗', async () => {
    vi.mocked(scaleDeployment).mockResolvedValue({ deployment_id: 'd1', copies: 3, status: 'running' })
    const wrapper = mountDialog()
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    await setCopies(wrapper, 3)
    buttonByText('确认调整')!.click()

    await vi.waitFor(() => expect(scaleDeployment).toHaveBeenCalledWith('d1', 3))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已提交:扩容至 3 份,进度随编排状态推进'))
    expect(onScaled).toHaveBeenCalledTimes(1)
    expect(closeSpy).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('提交失败(如 409 防重入):提示走 HTTP 层,弹窗保留可重试,不回调不关窗', async () => {
    vi.mocked(scaleDeployment).mockRejectedValue(new Error('409'))
    const wrapper = mountDialog()
    await vi.waitFor(() => expect(document.body.textContent).toContain('可扩上限'))
    await setCopies(wrapper, 1)
    buttonByText('确认调整')!.click()

    await vi.waitFor(() => expect(scaleDeployment).toHaveBeenCalledWith('d1', 1))
    await nextTick()
    expect(message.success).not.toHaveBeenCalled()
    expect(onScaled).not.toHaveBeenCalled()
    expect(closeSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
