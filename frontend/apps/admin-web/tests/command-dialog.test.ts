// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/api/deployment', () => ({
  getInstanceCommand: vi.fn(),
}))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import { getInstanceCommand, type InstanceCommand } from '@/api/deployment'
import { message } from '@repo/hooks'
import CommandDialog from '@/views/deploy/components/deploy-list/deploy-detail/components/CommandDialog.vue'

function makeCommand(overrides: Partial<InstanceCommand> = {}): InstanceCommand {
  return {
    found: true,
    command:
      'docker run -d \\\n  --name llmpod-d1-i0 \\\n  --network host \\\n  vllm/vllm-openai:v0.23.0 --model /models/qwen',
    image: 'vllm/vllm-openai:v0.23.0',
    container_name: 'llmpod-d1-i0',
    task_status: 'SUCCESS',
    attempt: 1,
    dispatched_at: '2026-09-07T12:00:00+00:00',
    ...overrides,
  }
}

/** navigator.clipboard.writeText 桩(happy-dom 无实现) */
const clipboardWriteText = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  clipboardWriteText.mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: clipboardWriteText },
    configurable: true,
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('CommandDialog 启动命令弹窗', () => {
  it('挂载拉一次该实例命令(不轮询),渲染完整 docker run 与元信息行', async () => {
    vi.mocked(getInstanceCommand).mockResolvedValue(makeCommand())
    const wrapper = mount(CommandDialog, {
      props: { deploymentId: 'd1', instanceId: 'i0' },
      attachTo: document.body,
    })

    expect(getInstanceCommand).toHaveBeenCalledWith('d1', 'i0')
    await vi.waitFor(() => expect(document.body.textContent).toContain('docker run -d'))

    const text = document.body.textContent ?? ''
    // 续行命令原样渲染(含续行符所在多行形态,white-space: pre-wrap)
    expect(text).toContain('--network host')
    expect(text).toContain('--model /models/qwen')
    // 元信息行:镜像/容器名/下发任务/时间
    expect(text).toContain('vllm/vllm-openai:v0.23.0')
    expect(text).toContain('llmpod-d1-i0')
    expect(text).toContain('SUCCESS')
    expect(text).toContain('第 1 次')

    // 命令不可变:挂载后不再重复拉取
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(getInstanceCommand).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('复制按钮:写整段命令到剪贴板,成功后短暂显示「已复制」', async () => {
    vi.mocked(getInstanceCommand).mockResolvedValue(makeCommand())
    const wrapper = mount(CommandDialog, {
      props: { deploymentId: 'd1', instanceId: 'i0' },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(document.body.textContent).toContain('docker run -d'))

    const copyBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('复制'),
    )!
    copyBtn.click()
    await vi.waitFor(() =>
      expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining('docker run -d')),
    )
    await vi.waitFor(() => expect(document.body.textContent).toContain('已复制'))
    wrapper.unmount()
  })

  it('found=false:空态文案(重启窗口/未下发过),不渲染命令块', async () => {
    vi.mocked(getInstanceCommand).mockResolvedValue(makeCommand({ found: false, command: null }))
    const wrapper = mount(CommandDialog, {
      props: { deploymentId: 'd1', instanceId: 'i0' },
      attachTo: document.body,
    })
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('暂无启动命令'),
    )
    expect(document.body.textContent).not.toContain('docker run')
    wrapper.unmount()
  })

  it('接口异常:HTTP 层已统一报错,弹窗静默空态', async () => {
    vi.mocked(getInstanceCommand).mockRejectedValue(new Error('boom'))
    const wrapper = mount(CommandDialog, {
      props: { deploymentId: 'd1', instanceId: 'i0' },
      attachTo: document.body,
    })
    await vi.waitFor(() => expect(document.body.textContent).toContain('暂无启动命令'))
    expect(message.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
