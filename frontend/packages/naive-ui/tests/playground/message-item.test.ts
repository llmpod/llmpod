// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { h, reactive } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import MessageItem from '../../src/components/playground/MessageItem.vue'
import type { PlaygroundChatMessage } from '../../src'
import { mountWithProvider } from '../helpers/mount'

/** 构造消息对象(reactive,模拟 usePlayground 内部状态)。 */
function createMessage(partial: Partial<PlaygroundChatMessage> & { role: 'user' | 'assistant' }) {
  return reactive({
    id: 1,
    content: '',
    reasoning: '',
    images: [],
    status: 'done',
    error: '',
    ...partial,
  })
}

interface MountedItem {
  wrapper: ReturnType<typeof mount>
  /** 捕获到的事件(按事件名聚合参数)。 */
  emitted: Record<string, unknown[][]>
}

/** 挂载 MessageItem(NaiveProvider 内,提供 message 上下文),并捕获其 emit 事件。 */
function mountItem(message: PlaygroundChatMessage, props: Record<string, unknown> = {}): MountedItem {
  const emitted: Record<string, unknown[][]> = {}
  const record = (name: string) => (...args: unknown[]) => {
    ;(emitted[name] ??= []).push(args)
  }
  const wrapper = mountWithProvider(() =>
    h(MessageItem, {
      message,
      ...props,
      onDelete: record('delete'),
      onResend: record('resend'),
      onRegenerate: record('regenerate'),
    }),
  )
  return { wrapper, emitted }
}

describe('MessageItem', () => {
  it('流式增量修改 reactive 消息,DOM 逐次更新(端到端修复验证)', async () => {
    const message = createMessage({ role: 'assistant', status: 'streaming' })
    const { wrapper } = mountItem(message, { streaming: true })

    message.content = '第一段'
    await vi.waitFor(() => expect(wrapper.text()).toContain('第一段'))

    message.content = '第一段第二段'
    await vi.waitFor(() => expect(wrapper.text()).toContain('第一段第二段'))
    expect(wrapper.find('.msg-item__typing').exists()).toBe(false)
  })

  it('用户消息:操作栏含复制/编辑/删除,点击删除触发 delete', async () => {
    const message = createMessage({ role: 'user', content: '你好' })
    const { wrapper, emitted } = mountItem(message)

    const titles = wrapper.findAll('button').map((btn) => btn.attributes('title'))
    expect(titles).toContain('复制')
    expect(titles).toContain('编辑后重新发送')
    expect(titles).toContain('删除')

    await wrapper.find('button[title="删除"]').trigger('click')
    expect(emitted.delete).toHaveLength(1)
  })

  it('用户消息:编辑态展示原文草稿,发送触发 resend', async () => {
    const message = createMessage({ role: 'user', content: '原文本' })
    const { wrapper, emitted } = mountItem(message)

    await wrapper.find('button[title="编辑后重新发送"]').trigger('click')
    const textarea = wrapper.find('textarea')
    expect(textarea.exists()).toBe(true)
    expect((textarea.element as HTMLTextAreaElement).value).toBe('原文本')

    await textarea.setValue('改后文本')
    const send = wrapper.findAll('button').find((btn) => btn.text() === '发送')
    expect(send).toBeDefined()
    await send!.trigger('click')

    expect(emitted.resend).toEqual([['改后文本']])
    // 发送后退出编辑态
    expect(wrapper.find('textarea').exists()).toBe(false)
  })

  it('用户消息:编辑空文本且有图片时仍可发送', async () => {
    const message = createMessage({
      role: 'user',
      content: '',
      images: ['data:image/png;base64,AAA'],
    })
    const { wrapper, emitted } = mountItem(message)

    await wrapper.find('button[title="编辑后重新发送"]').trigger('click')
    const textarea = wrapper.find('textarea')
    await textarea.setValue('')
    const send = wrapper.findAll('button').find((btn) => btn.text() === '发送')
    expect(send?.attributes('disabled')).toBeUndefined()

    await send!.trigger('click')
    expect(emitted.resend).toEqual([['']])
  })

  it('用户消息:编辑可取消,不触发 resend', async () => {
    const message = createMessage({ role: 'user', content: '原文本' })
    const { wrapper, emitted } = mountItem(message)

    await wrapper.find('button[title="编辑后重新发送"]').trigger('click')
    await wrapper.find('textarea').setValue('改了但不发')
    const cancel = wrapper.findAll('button').find((btn) => btn.text() === '取消')
    await cancel!.trigger('click')

    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(emitted.resend).toBeUndefined()
    expect(wrapper.text()).toContain('原文本')
  })

  it('AI 消息:操作栏含复制全文/重新生成/删除', async () => {
    const message = createMessage({ role: 'assistant', content: '回答' })
    const { wrapper, emitted } = mountItem(message)

    await wrapper.find('button[title="重新生成"]').trigger('click')
    await wrapper.find('button[title="删除"]').trigger('click')
    expect(emitted.regenerate).toHaveLength(1)
    expect(emitted.delete).toHaveLength(1)
  })

  it('对话消息渲染头像:assistant 为项目 logo,user 为人像图标', () => {
    const assistant = mountItem(createMessage({ role: 'assistant', content: '回答' }))
    const assistantAvatar = assistant.wrapper.find('.msg-item__avatar--assistant')
    expect(assistantAvatar.exists()).toBe(true)
    const logo = assistantAvatar.find('svg')
    expect(logo.exists()).toBe(true)
    expect(logo.attributes('viewBox')).toBe('0 0 320 350.8')

    const user = mountItem(createMessage({ role: 'user', content: '你好' }))
    const userAvatar = user.wrapper.find('.msg-item__avatar--user')
    expect(userAvatar.exists()).toBe(true)
    expect(userAvatar.find('svg').exists()).toBe(true)
  })

  it('actionsDisabled 或流式中不渲染操作栏', () => {
    const streaming = createMessage({ role: 'assistant', content: '回答', status: 'streaming' })
    const streamingResult = mountItem(streaming, { streaming: true })
    expect(streamingResult.wrapper.find('button[title="删除"]').exists()).toBe(false)

    const disabledResult = mountItem(createMessage({ role: 'user', content: '你好' }), {
      actionsDisabled: true,
    })
    expect(disabledResult.wrapper.find('button[title="删除"]').exists()).toBe(false)
  })

  it('错误消息:渲染错误原因与 OpenAI 标准错误标签(HTTP 状态码/type/code)', () => {
    const message = createMessage({
      role: 'assistant',
      status: 'error',
      error: 'The model `nope` does not exist',
      errorDetail: { status: 404, type: 'invalid_request_error', code: 'model_not_found' },
    })
    const { wrapper } = mountItem(message)

    expect(wrapper.find('.msg-item__error').exists()).toBe(true)
    expect(wrapper.text()).toContain('The model `nope` does not exist')
    expect(wrapper.text()).toContain('HTTP 404')
    expect(wrapper.text()).toContain('invalid_request_error')
    expect(wrapper.text()).toContain('model_not_found')
  })

  it('错误消息:无详情时只渲染原因,不渲染标签行', () => {
    const message = createMessage({ role: 'assistant', status: 'error', error: '模型不存在' })
    const { wrapper } = mountItem(message)

    expect(wrapper.text()).toContain('模型不存在')
    expect(wrapper.find('.msg-item__error-tags').exists()).toBe(false)
  })

  it('纯图片用户消息不显示复制按钮(无文本)', () => {
    const { wrapper } = mountItem(
      createMessage({ role: 'user', images: ['data:image/png;base64,AAA'] }),
    )
    const titles = wrapper.findAll('button').map((btn) => btn.attributes('title'))
    expect(titles).not.toContain('复制')
  })
})
