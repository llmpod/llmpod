// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyToClipboard } from '../../src/utils/clipboard'

/**
 * 覆盖 navigator.clipboard:传 writeText 模拟安全上下文(HTTPS);
 * 不传(undefined)模拟非安全上下文——HTTP 部署时浏览器不给挂 clipboard。
 */
function stubClipboard(writeText?: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  })
}

/** 覆盖 document.execCommand(happy-dom 未实现,回退分支桩);返回桩内捕获的 textarea */
function stubExecCommand(impl: () => boolean) {
  const execCommand = vi.fn(impl)
  Object.defineProperty(document, 'execCommand', {
    value: execCommand,
    configurable: true,
    writable: true,
  })
  return execCommand
}

afterEach(() => {
  vi.restoreAllMocks()
  stubClipboard(undefined)
  document.body.innerHTML = ''
})

describe('copyToClipboard 剪贴板复制', () => {
  it('安全上下文:直接走 Clipboard API,不触碰 execCommand', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)
    const execCommand = stubExecCommand(() => true)

    await expect(copyToClipboard('abc')).resolves.toBeUndefined()

    expect(writeText).toHaveBeenCalledWith('abc')
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('非安全上下文(HTTP 部署,无 navigator.clipboard):回退 execCommand 复制成功', async () => {
    stubClipboard(undefined)
    let textareaAtCopy: HTMLTextAreaElement | null = null
    const execCommand = stubExecCommand(() => {
      textareaAtCopy = document.querySelector('textarea')
      return true
    })

    await expect(copyToClipboard('docker run -d qwen')).resolves.toBeUndefined()

    expect(execCommand).toHaveBeenCalledWith('copy')
    // 回退载体:离屏只读 textarea 承载完整文本,用后即删不留 DOM 残留
    expect(textareaAtCopy?.value).toBe('docker run -d qwen')
    expect(textareaAtCopy?.hasAttribute('readonly')).toBe(true)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('弹窗/抽屉场景(focus trap):textarea 挂到当前焦点元素内部而非 body', async () => {
    stubClipboard(undefined)
    const btn = document.createElement('button')
    document.body.appendChild(btn)
    btn.focus()
    let hostAtCopy: Element | null = null
    stubExecCommand(() => {
      hostAtCopy = document.querySelector('textarea')?.parentElement ?? null
      return true
    })

    await expect(copyToClipboard('abc')).resolves.toBeUndefined()

    // n-modal/n-drawer 默认 trapFocus(vueuc FocusTrap 捕获阶段监听 focus,焦点出弹窗即被拉回);
    // textarea 挂 body 会因获得焦点触发拦截破坏选区,挂到焦点元素(触发按钮)内部则转移合法
    expect(hostAtCopy).toBe(btn)
    expect(document.querySelector('textarea')).toBeNull()
    // 复制后还原触发元素焦点
    expect(document.activeElement).toBe(btn)
    btn.remove()
  })

  it('Clipboard API 被拒(权限/失焦):自动回退 execCommand 成功', async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error('denied')))
    const execCommand = stubExecCommand(() => true)

    await expect(copyToClipboard('abc')).resolves.toBeUndefined()

    expect(execCommand).toHaveBeenCalledWith('copy')
  })

  it('两条路径都失败(execCommand 返回 false):reject 交调用方提示', async () => {
    stubClipboard(undefined)
    stubExecCommand(() => false)

    await expect(copyToClipboard('abc')).rejects.toBeInstanceOf(Error)
  })
})
