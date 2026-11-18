// @vitest-environment happy-dom
import { mount, flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/version', () => ({
  checkUpdate: vi.fn(),
  getServerVersion: vi.fn(),
}))
vi.mock('@/api/nps', () => ({ submitNps: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, openNpsFeedback: vi.fn() }
})

import { openNpsFeedback } from '@repo/hooks'
import { checkUpdate, getServerVersion, type VersionLatestData } from '@/api/version'
import { submitNps } from '@/api/nps'
import AboutPage from '@/views/about/index.vue'

/** 无更新的版本信息:让关于页停在常态,不渲染新版本面板干扰断言 */
const VERSION_FIXTURE: VersionLatestData = {
  current_version: '2.0.0',
  latest_version: '2.0.0',
  has_update: false,
  update_check_enabled: true,
  checked_at: '2026-01-01T00:00:00Z',
  notes: null,
  release_url: null,
  published_at: null,
  error_message: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(getServerVersion).mockReturnValue(VERSION_FIXTURE.current_version)
  vi.mocked(checkUpdate).mockResolvedValue({ ...VERSION_FIXTURE })
})

/** 挂载关于页并等进入页面的静默版本检查落地 */
async function mountAbout(): Promise<VueWrapper> {
  const wrapper = mount(AboutPage)
  await flushPromises()
  return wrapper
}

function buttonByText(wrapper: VueWrapper, text: string) {
  const btn = wrapper.findAll('button').find((item) => item.text().includes(text))
  expect(btn, `未找到按钮:${text}`).toBeTruthy()
  return btn!
}

describe('关于页 NPS 入口', () => {
  it('Hero 按钮区渲染「打个分」,与「检查更新」同排', async () => {
    const wrapper = await mountAbout()

    const npsBtn = buttonByText(wrapper, '打个分')
    const checkBtn = buttonByText(wrapper, '检查更新')

    expect(npsBtn.element.parentElement).toBe(checkBtn.element.parentElement)
    wrapper.unmount()
  })

  it('点击唤起 NPS 弹窗:onSubmit 调 /admin/nps,score/comment 映射为 rate/comment', async () => {
    const wrapper = await mountAbout()
    vi.mocked(submitNps).mockResolvedValue(null)

    await buttonByText(wrapper, '打个分').trigger('click')

    expect(openNpsFeedback).toHaveBeenCalledTimes(1)
    const options = vi.mocked(openNpsFeedback).mock.calls[0][0]!
    expect(typeof options.onSubmit).toBe('function')

    await options.onSubmit?.({ score: 5, comment: '希望支持批量部署' })
    expect(submitNps).toHaveBeenCalledWith({ rate: 5, comment: '希望支持批量部署' })

    wrapper.unmount()
  })

  it('接口失败时不吞异常:错误继续抛给弹窗,由弹窗保持打开并提示重试', async () => {
    const wrapper = await mountAbout()
    vi.mocked(submitNps).mockRejectedValue(new Error('反馈提交失败：错误的参数【SERVER_ID】'))

    await buttonByText(wrapper, '打个分').trigger('click')
    const options = vi.mocked(openNpsFeedback).mock.calls[0][0]!

    await expect(options.onSubmit?.({ score: 3, comment: null })).rejects.toThrow('反馈提交失败')

    wrapper.unmount()
  })
})
