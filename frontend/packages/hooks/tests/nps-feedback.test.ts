// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import { closeAllDialog, openNpsFeedback } from '../src'
import { mountWithHost } from './helpers/mount'

let wrapper: VueWrapper | null = null

afterEach(() => {
  // 命令式弹窗队列是模块级的，必须清空，否则跨用例会累积多个弹窗
  closeAllDialog()
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

/** 打开弹窗并挂载宿主；命令式弹窗依赖 DialogHost 才能渲染 */
async function open(options: Parameters<typeof openNpsFeedback>[0] = {}) {
  wrapper = mountWithHost(() => h('div'))
  const handle = openNpsFeedback(options)
  await nextTick()
  return handle
}

function buttonByText(text: string): HTMLButtonElement {
  const btn = [...document.querySelectorAll<HTMLButtonElement>('.n-button')].find((item) =>
    item.textContent?.includes(text),
  )
  expect(btn, `未找到按钮：${text}`).toBeTruthy()
  return btn!
}

async function clickStar(value: number): Promise<void> {
  const items = document.querySelectorAll<HTMLElement>('.n-rate__item')
  expect(items.length, '星级未渲染').toBe(5)
  items[value - 1].click()
  await nextTick()
}

describe('openNpsFeedback', () => {
  it('默认标题用品牌名拼 NPS 问法，按钮为「提交 / 稍后再说」', async () => {
    await open({ onSubmit: vi.fn() })

    expect(document.querySelector('.lm-dialog__title')?.textContent).toContain(
      '推荐给同事，你会打几分',
    )
    expect(buttonByText('提交')).toBeTruthy()
    expect(buttonByText('稍后再说')).toBeTruthy()
  })

  it('未打分就提交：内联提示先选分数，且不触发 onSubmit', async () => {
    const onSubmit = vi.fn()
    await open({ onSubmit })

    buttonByText('提交').click()
    await nextTick()
    await nextTick()

    expect(document.body.textContent).toContain('请先选择一个分数')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('打分后提交：onSubmit 收到分数与文本', async () => {
    const onSubmit = vi.fn()
    await open({ onSubmit })

    await clickStar(4)
    const textarea = document.querySelector('textarea')!
    textarea.value = '希望支持批量部署'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    buttonByText('提交').click()
    await nextTick()
    await nextTick()

    expect(onSubmit).toHaveBeenCalledWith({ score: 4, comment: '希望支持批量部署' })
  })

  it('只打分不填文本时，comment 归为 null', async () => {
    const onSubmit = vi.fn()
    await open({ onSubmit })

    await clickStar(5)
    buttonByText('提交').click()
    await nextTick()
    await nextTick()

    expect(onSubmit).toHaveBeenCalledWith({ score: 5, comment: null })
  })

  it('低分时追问切成问原因', async () => {
    await open({ onSubmit: vi.fn() })

    await clickStar(2)

    expect(document.body.textContent).toContain('具体是哪里让你不太满意')
    expect(document.body.textContent).toContain('不太满意')
  })

  it('点「稍后再说」直接关闭，不触发 onSubmit', async () => {
    const onSubmit = vi.fn()
    await open({ onSubmit })

    await clickStar(4)
    buttonByText('稍后再说').click()
    await nextTick()
    await nextTick()

    expect(onSubmit).not.toHaveBeenCalled()
    expect(document.querySelector('.lm-dialog')).toBeNull()
  })

  it('onSubmit 抛错时保留弹窗并提示失败', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('boom'))
    await open({ onSubmit })

    await clickStar(3)
    buttonByText('提交').click()
    await nextTick()
    await nextTick()

    expect(document.body.textContent).toContain('提交失败，请稍后再试')
    expect(document.querySelector('.lm-dialog')).not.toBeNull()
  })

  it('标题与宽度可覆盖', async () => {
    await open({ title: '自定义标题', area: 52 })

    expect(document.querySelector('.lm-dialog__title')?.textContent).toBe('自定义标题')
    const dialog = document.querySelector('.lm-dialog') as HTMLElement | null
    expect(dialog?.style.width).toBe('52rem')
  })
})
