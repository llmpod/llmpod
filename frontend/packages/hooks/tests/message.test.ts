// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeAllMessage, message, useMessage } from '../src'
import { mountWithHost } from './helpers/mount'

let wrapper: ReturnType<typeof mountWithHost> | null = null

beforeEach(() => {
  wrapper = mountWithHost(() => h('div'))
})

afterEach(() => {
  closeAllMessage()
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('useMessage 命令式提示', () => {
  it('渲染内容与类型,message.success 等价调用', async () => {
    message.success('保存成功')
    await nextTick()
    expect(document.body.textContent).toContain('保存成功')

    const handle = useMessage({ type: 'error', content: '出错了', duration: null })
    await nextTick()
    expect(document.body.textContent).toContain('出错了')
    handle.close()
  })

  it('close 移除提示并触发 onClose;closeAllMessage 清场', async () => {
    const onClose = vi.fn()
    const handle = useMessage({ content: '待关闭', duration: null, onClose })
    await nextTick()
    expect(document.body.textContent).toContain('待关闭')

    handle.close()
    await nextTick()
    expect(document.body.textContent).not.toContain('待关闭')
    expect(onClose).toHaveBeenCalledTimes(1)

    message.info('a', { duration: null })
    message.warning('b', { duration: null })
    await nextTick()
    closeAllMessage()
    await nextTick()
    expect(document.body.textContent).not.toContain('a')
    expect(document.body.textContent).not.toContain('b')
  })

  it('默认 3000ms 自动关闭;duration:null 不自动关闭;自定义时长生效', async () => {
    vi.useFakeTimers()
    message.info('三秒自动关')
    await nextTick()
    expect(document.body.textContent).toContain('三秒自动关')
    await vi.advanceTimersByTimeAsync(3000)
    expect(document.body.textContent).not.toContain('三秒自动关')

    message.info('不自动关', { duration: null })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(document.body.textContent).toContain('不自动关')

    message.info('一百毫秒关', { duration: 100 })
    await vi.advanceTimersByTimeAsync(100)
    expect(document.body.textContent).not.toContain('一百毫秒关')
  })

  it('提前 close 清除定时器,不会到期重复关闭', async () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    const handle = useMessage({ content: '提前关', duration: 5000, onClose })
    await nextTick()
    handle.close()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('队列挂 globalThis 单一记录(message 与 MessageHost 共享同一队列)', async () => {
    message.info('共享队列')
    await nextTick()
    const store = (globalThis as unknown as Record<string, { queue: { value: unknown[] } }>)[
      '__lm_message__'
    ]
    expect(store).toBeTruthy()
    expect(store.queue.value.length).toBeGreaterThan(0)
    expect(document.body.textContent).toContain('共享队列')
  })

  it('长文本(>50 字)默认不自动关闭且显示关闭按钮,可手动关闭', async () => {
    vi.useFakeTimers()
    const longContent = '长'.repeat(51)
    const handle = message.error(longContent)
    await nextTick()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(document.body.textContent).toContain(longContent)

    const closeBtn = document.querySelector('.lm-message__close')
    expect(closeBtn).toBeTruthy()
    ;(closeBtn as HTMLElement).click()
    await nextTick()
    expect(document.body.textContent).not.toContain(longContent)
    handle.close()
  })

  it('长文本显式传 duration/closable 以用户为准;恰好 50 字按短文本处理', async () => {
    vi.useFakeTimers()
    const longContent = '长'.repeat(51)
    message.info(longContent, { duration: 100 })
    await vi.advanceTimersByTimeAsync(100)
    expect(document.body.textContent).not.toContain(longContent)

    const bounded = '五'.repeat(50)
    message.info(bounded)
    await vi.advanceTimersByTimeAsync(3000)
    expect(document.body.textContent).not.toContain(bounded)
    expect(document.querySelector('.lm-message__close')).toBeNull()
  })

  it('各类型方法映射正确类型标记', async () => {
    message.info('i', { duration: null })
    message.warning('w', { duration: null })
    message.error('e', { duration: null })
    message.loading('l', { duration: null })
    await nextTick()
    const store = (globalThis as unknown as Record<string, { queue: { value: { state: { type: string } }[] } }>)[
      '__lm_message__'
    ]
    const types = store.queue.value.map((item) => item.state.type)
    expect(types).toEqual(['info', 'warning', 'error', 'loading'])
  })
})
