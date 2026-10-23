// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'
import { applyModelDefaults, usePlayground } from '../../src'
import type { PlaygroundChatCallbacks, PlaygroundChatSender, PlaygroundParams } from '../../src'

/** 默认示例对话的 assistant 回复。 */
const DEFAULT_REPLY = '你好，请问有什么可以帮助您的吗？'

/** 构造默认参数(与 usePlayground 初始值一致)。 */
function defaultParams(): PlaygroundParams {
  return {
    model: 'glm-5.2',
    mode: 'chat',
    stream: true,
    temperature: 0.7,
    maxTokens: 4096,
    topP: 0.9,
    systemPrompt: '',
    reasoningEffort: 'default',
  }
}

/** 一次 sender 调用的快照。 */
interface SenderCall {
  params: PlaygroundParams
  messages: unknown[]
  callbacks: PlaygroundChatCallbacks
}

/**
 * 构造可控 sender:harness.resolve 控制 sender 何时结束。
 */
function createSenderHarness() {
  const calls: SenderCall[] = []
  const harness = {
    calls,
    /** 挂起 sender 直到调用 resolve。 */
    resolve: (() => {}) as () => void,
    sender: null as unknown as PlaygroundChatSender,
  }
  harness.sender = vi.fn(async (params, messages, callbacks) => {
    calls.push({ params: { ...params }, messages, callbacks })
    await new Promise<void>((resolve) => {
      harness.resolve = resolve
    })
  })
  return harness
}

/** 等待微任务/宏任务排空(FileReader 等异步)。 */
function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function createPlayground(sender: PlaygroundChatSender, onError?: (msg: string) => void) {
  const playground = usePlayground({ sender, onError })
  playground.params.model = 'glm-5.2'
  return playground
}

describe('applyModelDefaults', () => {
  /** 构造带用户手调痕迹的参数:温度/maxTokens/思考强度均已偏离默认。 */
  function tunedParams(): PlaygroundParams {
    return {
      ...defaultParams(),
      temperature: 0.2,
      maxTokens: 8000,
      reasoningEffort: 'high',
    }
  }

  it('reasoning=false:思考强度填 none,不传 reasoning_effort 的档位被覆盖', () => {
    const params = tunedParams()
    applyModelDefaults(params, { label: 'm', value: 'm', reasoning: false })
    expect(params.reasoningEffort).toBe('none')
  })

  it('reasoning=true:思考强度填 default(模型自行决定)', () => {
    const params = tunedParams()
    applyModelDefaults(params, { label: 'm', value: 'm', reasoning: true })
    expect(params.reasoningEffort).toBe('default')
  })

  it('未传 reasoning:思考强度保持用户当前值', () => {
    const params = tunedParams()
    applyModelDefaults(params, { label: 'm', value: 'm' })
    expect(params.reasoningEffort).toBe('high')
  })

  it('最大 Token 不再从模型列表联动:切换模型保持用户当前值(默认 4096)', () => {
    const params = tunedParams()
    applyModelDefaults(params, { label: 'm', value: 'm' })
    expect(params.maxTokens).toBe(8000)
  })

  it('仅填模型特有字段:温度/Top P/系统提示词/流式开关均不动', () => {
    const params = tunedParams()
    applyModelDefaults(params, {
      label: 'm',
      value: 'm',
      reasoning: false,
    })
    expect(params.temperature).toBe(0.2)
    expect(params.topP).toBe(0.9)
    expect(params.systemPrompt).toBe('')
    expect(params.stream).toBe(true)
    expect(params.model).toBe('glm-5.2')
  })
})

describe('usePlayground', () => {
  it('默认示例对话:初始化即包含 user 你好 + assistant 回复', () => {
    const { messages } = createPlayground(vi.fn())
    expect(messages.value).toHaveLength(2)
    expect(messages.value[0]).toMatchObject({ role: 'user', content: '你好', images: [] })
    expect(messages.value[1]).toMatchObject({ role: 'assistant', content: DEFAULT_REPLY })
  })

  it('发送文本:追加 user 消息与 assistant 占位,sender 收到含示例对话的历史', async () => {
    const harness = createSenderHarness()
    const { input, messages, sending, send } = createPlayground(harness.sender)

    input.value = '今天天气如何'
    const promise = send()
    expect(sending.value).toBe(true)
    expect(messages.value).toHaveLength(4)
    expect(messages.value[2]).toMatchObject({ role: 'user', content: '今天天气如何', images: [] })
    expect(messages.value[3]).toMatchObject({ role: 'assistant', status: 'streaming' })
    expect(harness.calls[0].messages).toEqual([
      { role: 'user', content: '你好' },
      { role: 'assistant', content: DEFAULT_REPLY },
      { role: 'user', content: '今天天气如何' },
    ])

    harness.resolve()
    await promise
    expect(sending.value).toBe(false)
  })

  it('带图片发送:user 消息转 multipart(text + image_url)', async () => {
    const harness = createSenderHarness()
    const { input, pendingImages, send } = createPlayground(harness.sender)

    input.value = '这是什么'
    pendingImages.value.push('data:image/png;base64,AAA', 'data:image/jpeg;base64,BBB')
    const promise = send()

    expect(harness.calls[0].messages[2]).toEqual({
      role: 'user',
      content: [
        { type: 'text', text: '这是什么' },
        { type: 'image_url', image_url: { url: 'data:image/png;base64,AAA' } },
        { type: 'image_url', image_url: { url: 'data:image/jpeg;base64,BBB' } },
      ],
    })
    expect(pendingImages.value).toHaveLength(0)

    harness.resolve()
    await promise
  })

  it('仅图片无文本:multipart 只含 image_url', async () => {
    const harness = createSenderHarness()
    const { pendingImages, send } = createPlayground(harness.sender)

    pendingImages.value.push('data:image/png;base64,AAA')
    const promise = send()

    expect(harness.calls[0].messages[2]).toEqual({
      role: 'user',
      content: [{ type: 'image_url', image_url: { url: 'data:image/png;base64,AAA' } }],
    })

    harness.resolve()
    await promise
  })

  it('onChunk 增量更新:think 标签内容拆入 reasoning,其余为正文', async () => {
    const harness = createSenderHarness()
    const { input, messages, send } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()

    const { onChunk } = harness.calls[0].callbacks
    onChunk('<think>先分析')
    let assistant = messages.value[3]
    expect(assistant.reasoning).toBe('先分析')
    expect(assistant.content).toBe('')

    onChunk('问题</think>答案是 42')
    assistant = messages.value[3]
    expect(assistant.reasoning).toBe('先分析问题')
    expect(assistant.content).toBe('答案是 42')

    harness.resolve()
    await promise
    expect(messages.value[3].status).toBe('done')
  })

  it('流式增量实时触发响应式更新(修复整段一次性渲染)', async () => {
    const harness = createSenderHarness()
    const { input, messages, send } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()

    const seen: string[] = []
    const stopWatch = watch(
      () => messages.value[3]?.content,
      (content) => seen.push(content ?? ''),
      { flush: 'sync' },
    )

    const { onChunk } = harness.calls[0].callbacks
    onChunk('你')
    onChunk('好')
    stopWatch()
    harness.resolve()
    await promise

    expect(seen).toEqual(['你', '你好'])
  })

  it('onReasoning 增量累积,与 think 标签解析结果合并', async () => {
    const harness = createSenderHarness()
    const { input, messages, send } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()

    const { onChunk, onReasoning } = harness.calls[0].callbacks
    onReasoning?.('字段思考')
    onReasoning?.('继续')
    onChunk('<think>标签思考</think>正文')

    const assistant = messages.value[3]
    expect(assistant.reasoning).toBe('字段思考继续标签思考')
    expect(assistant.content).toBe('正文')

    harness.resolve()
    await promise
  })

  it('onRestart 清空累积缓冲:流式降级后新全文替换,不与已消费增量拼接(UR-006)', async () => {
    const sender: PlaygroundChatSender = async (_params, _messages, callbacks) => {
      // 模拟 chat-sender 流式中断降级:先推半截增量,onRestart 后一次性推全文
      callbacks.onChunk('<think>半截思考')
      callbacks.onReasoning?.('半截字段思考')
      callbacks.onRestart?.()
      callbacks.onChunk('完整回答')
      callbacks.onReasoning?.('完整字段思考')
    }
    const { input, messages, send } = createPlayground(sender)

    input.value = '问题'
    await send()

    expect(messages.value[3]).toMatchObject({ content: '完整回答', status: 'done' })
    expect(messages.value[3].reasoning).toBe('完整字段思考')
  })

  it('历史回传:assistant 只回传可见正文,思考过程不回传', async () => {
    const harness = createSenderHarness()
    const { input, messages, send } = createPlayground(harness.sender)

    // 第一轮:带思考的完整回复
    input.value = '第一问'
    let promise = send()
    const first = harness.calls[0].callbacks
    first.onChunk('<think>思考</think>第一轮回答')
    harness.resolve()
    await promise
    expect(messages.value[3]).toMatchObject({ reasoning: '思考', content: '第一轮回答' })

    // 第二轮:历史里的 assistant 消息应是纯正文
    input.value = '第二问'
    promise = send()
    expect(harness.calls[1].messages[3]).toEqual({ role: 'assistant', content: '第一轮回答' })
    harness.resolve()
    await promise
  })

  it('stream 开关随参数透传给 sender', async () => {
    const harness = createSenderHarness()
    const { input, params, send } = createPlayground(harness.sender)

    params.stream = false
    input.value = '问题'
    const promise = send()
    expect(harness.calls[0].params.stream).toBe(false)
    harness.resolve()
    await promise
  })

  it('停止生成:保留已生成内容并标记 stopped', async () => {
    const harness = createSenderHarness()
    const { input, messages, send, stop } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()

    harness.calls[0].callbacks.onChunk('已生成的部分')
    stop()
    harness.resolve()
    await promise

    expect(messages.value[3]).toMatchObject({ content: '已生成的部分', status: 'stopped' })
  })

  it('停止时无任何内容:移除 assistant 占位', async () => {
    const harness = createSenderHarness()
    const { input, messages, send, stop } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()

    stop()
    harness.resolve()
    await promise

    // 剩默认示例对话 + 本轮 user 消息
    expect(messages.value).toHaveLength(3)
    expect(messages.value[2].role).toBe('user')
  })

  it('空响应:移除 assistant 占位消息', async () => {
    const harness = createSenderHarness()
    const { input, messages, send } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()

    harness.resolve()
    await promise
    expect(messages.value).toHaveLength(3)
  })

  it('无内容时失败:保留占位消息并在内容区标记 error(不走 onError/toast)', async () => {
    const onError = vi.fn()
    const failingSender = vi.fn(async () => {
      throw new Error('模型不存在')
    })
    const { input, messages, send } = createPlayground(
      failingSender as PlaygroundChatSender,
      onError,
    )

    input.value = '问题'
    await send()
    expect(messages.value).toHaveLength(4)
    expect(messages.value[3]).toMatchObject({
      role: 'assistant',
      content: '',
      status: 'error',
      error: '模型不存在',
    })
    expect(messages.value[3].errorDetail).toBeNull()
    expect(onError).not.toHaveBeenCalled()
  })

  it('OpenAI 标准错误:提取 error.message 原因与 type/code/状态码详情', async () => {
    // 模拟 openai SDK APIError:标准错误体 { error: { message, type, param, code } } 提升出
    // 顶层 status/type/code/param,message 为 `${status} ${内层 message}`
    const apiError = Object.assign(
      new Error('404 The model `nope` does not exist'),
      {
        status: 404,
        type: 'invalid_request_error',
        code: 'model_not_found',
        param: null,
        error: {
          message: 'The model `nope` does not exist',
          type: 'invalid_request_error',
          param: null,
          code: 'model_not_found',
        },
      },
    )
    const failingSender = vi.fn(async () => {
      throw apiError
    })
    const { input, messages, send } = createPlayground(failingSender as PlaygroundChatSender)

    input.value = '问题'
    await send()
    expect(messages.value[3]).toMatchObject({
      status: 'error',
      error: 'The model `nope` does not exist',
      errorDetail: {
        status: 404,
        type: 'invalid_request_error',
        code: 'model_not_found',
      },
    })
  })

  it('网关 200/SSE 错误帧:错误体挂在 error 字段时同样解析出原因与详情', async () => {
    // 模拟 chat-sender 的 GatewayError:网关以 200 响应/SSE 数据帧返回标准错误体
    const gatewayError = Object.assign(new Error('当前分组下无可用渠道'), {
      error: { message: '当前分组下无可用渠道', type: 'new_api_error' },
    })
    const failingSender = vi.fn(async () => {
      throw gatewayError
    })
    const { input, messages, send } = createPlayground(failingSender as PlaygroundChatSender)

    input.value = '问题'
    await send()
    expect(messages.value[3]).toMatchObject({
      status: 'error',
      error: '当前分组下无可用渠道',
      errorDetail: { type: 'new_api_error' },
    })
  })

  it('网络错误:cause 底层原因并入错误文案', async () => {
    // 模拟 SDK APIConnectionError:message 为固定文案,真实原因在 cause
    const connError = Object.assign(new Error('Connection error.'), {
      cause: new TypeError('Failed to fetch'),
    })
    const failingSender = vi.fn(async () => {
      throw connError
    })
    const { input, messages, send } = createPlayground(failingSender as PlaygroundChatSender)

    input.value = '问题'
    await send()
    expect(messages.value[3]).toMatchObject({
      status: 'error',
      error: 'Connection error.(Failed to fetch)',
    })
    expect(messages.value[3].errorDetail).toBeNull()
  })

  it('无内容失败消息不进入后续请求历史', async () => {
    let shouldFail = true
    const harness = createSenderHarness()
    const sender: PlaygroundChatSender = async (params, messages, callbacks) => {
      harness.calls.push({ params: { ...params }, messages, callbacks })
      if (shouldFail) {
        shouldFail = false
        throw new Error('模型不存在')
      }
      await new Promise<void>((resolve) => {
        harness.resolve = resolve
      })
    }
    const { input, send } = createPlayground(sender)

    input.value = '问题'
    await send()

    input.value = '再问'
    const promise = send()
    expect(harness.calls[1].messages).toEqual([
      { role: 'user', content: '你好' },
      { role: 'assistant', content: DEFAULT_REPLY },
      { role: 'user', content: '问题' },
      { role: 'user', content: '再问' },
    ])
    harness.resolve()
    await promise
  })

  it('有部分内容时失败:保留内容,消息内标记 error 且不触发 onError', async () => {
    const onError = vi.fn()
    const failingSender = vi.fn(async (_params, _messages, callbacks) => {
      callbacks.onChunk('部分内容')
      throw new Error('连接中断')
    })
    const { input, messages, send } = createPlayground(
      failingSender as PlaygroundChatSender,
      onError,
    )

    input.value = '问题'
    await send()
    const assistant = messages.value[3]
    expect(assistant).toMatchObject({
      content: '部分内容',
      status: 'error',
      error: '连接中断',
    })
    expect(onError).not.toHaveBeenCalled()
  })

  it('清空:回到默认示例对话,待发送图片一并清除', async () => {
    const harness = createSenderHarness()
    const { input, messages, pendingImages, send, clear } = createPlayground(harness.sender)
    input.value = '问题'
    const promise = send()
    harness.calls[0].callbacks.onChunk('回答')
    harness.resolve()
    await promise

    pendingImages.value.push('data:image/png;base64,AAA')
    clear()
    expect(messages.value).toHaveLength(2)
    expect(messages.value[0]).toMatchObject({ role: 'user', content: '你好' })
    expect(messages.value[1]).toMatchObject({ role: 'assistant', content: DEFAULT_REPLY })
    expect(pendingImages.value).toHaveLength(0)
  })

  describe('消息操作', () => {
    it('删除单条消息', () => {
      const { messages, removeMessage } = createPlayground(vi.fn())
      removeMessage(messages.value[0])
      expect(messages.value).toHaveLength(1)
      expect(messages.value[0].role).toBe('assistant')
    })

    it('生成中禁止删除/编辑重发/重新生成', async () => {
      const harness = createSenderHarness()
      const { input, messages, send, removeMessage, resendMessage, regenerateMessage } =
        createPlayground(harness.sender)
      input.value = '问题'
      const promise = send()

      removeMessage(messages.value[0])
      resendMessage(messages.value[2], '改')
      regenerateMessage(messages.value[1])
      expect(messages.value).toHaveLength(4)

      harness.resolve()
      await promise
    })

    it('重新生成:移除目标回复及其后消息,基于剩余历史重新请求', async () => {
      const harness = createSenderHarness()
      const { input, messages, send, regenerateMessage } = createPlayground(harness.sender)

      input.value = '第一问'
      let promise = send()
      harness.calls[0].callbacks.onChunk('第一轮回答')
      harness.resolve()
      await promise

      input.value = '第二问'
      promise = send()
      harness.calls[1].callbacks.onChunk('第二轮回答')
      harness.resolve()
      await promise
      expect(messages.value).toHaveLength(6)

      // 对第一轮回答点"重新生成":其后消息(第二问/第二轮回答)一并移除
      promise = regenerateMessage(messages.value[3])
      expect(messages.value).toHaveLength(4)
      expect(messages.value[3]).toMatchObject({ role: 'assistant', status: 'streaming' })
      expect(harness.calls[2].messages).toEqual([
        { role: 'user', content: '你好' },
        { role: 'assistant', content: DEFAULT_REPLY },
        { role: 'user', content: '第一问' },
      ])

      harness.calls[2].callbacks.onChunk('重新生成的回答')
      harness.resolve()
      await promise
      expect(messages.value[3]).toMatchObject({ content: '重新生成的回答', status: 'done' })
    })

    it('编辑重发:更新 user 文本,移除其后消息并基于新内容重新生成', async () => {
      const harness = createSenderHarness()
      const { input, messages, send, resendMessage } = createPlayground(harness.sender)

      input.value = '第一问'
      let promise = send()
      harness.calls[0].callbacks.onChunk('第一轮回答')
      harness.resolve()
      await promise

      promise = resendMessage(messages.value[2], '改后的问题')
      expect(messages.value[2]).toMatchObject({ role: 'user', content: '改后的问题' })
      expect(messages.value).toHaveLength(4)
      expect(harness.calls[1].messages).toEqual([
        { role: 'user', content: '你好' },
        { role: 'assistant', content: DEFAULT_REPLY },
        { role: 'user', content: '改后的问题' },
      ])

      harness.calls[1].callbacks.onChunk('新的回答')
      harness.resolve()
      await promise
      expect(messages.value[3]).toMatchObject({ content: '新的回答', status: 'done' })
    })

    it('编辑重发:原图片保留,重发请求含图片 multipart', async () => {
      const harness = createSenderHarness()
      const { input, pendingImages, messages, send, resendMessage } =
        createPlayground(harness.sender)

      input.value = '看图'
      pendingImages.value.push('data:image/png;base64,AAA')
      let promise = send()
      harness.resolve()
      await promise

      promise = resendMessage(messages.value[2], '看图,再详细点')
      expect(harness.calls[1].messages[2]).toEqual({
        role: 'user',
        content: [
          { type: 'text', text: '看图,再详细点' },
          { type: 'image_url', image_url: { url: 'data:image/png;base64,AAA' } },
        ],
      })
      harness.resolve()
      await promise
    })

    it('编辑重发:空文本且无图片时拒绝', async () => {
      const harness = createSenderHarness()
      const { input, messages, send, resendMessage } = createPlayground(harness.sender)

      input.value = '第一问'
      let promise = send()
      harness.calls[0].callbacks.onChunk('第一轮回答')
      harness.resolve()
      await promise

      await resendMessage(messages.value[2], '')
      expect(harness.calls).toHaveLength(1)
      expect(messages.value).toHaveLength(4)
    })
  })

  describe('图片文件校验', () => {
    it('非图片文件被拒绝并提示', async () => {
      const onError = vi.fn()
      const { addImageFiles, pendingImages } = createPlayground(vi.fn(), onError)

      addImageFiles([new File(['x'], 'a.txt', { type: 'text/plain' })])
      await flush()

      expect(onError).toHaveBeenCalledWith('不支持的文件类型:a.txt')
      expect(pendingImages.value).toHaveLength(0)
    })

    it('超过大小上限被拒绝并提示', async () => {
      const onError = vi.fn()
      const { addImageFiles, pendingImages } = createPlayground(vi.fn(), onError)

      const bigFile = new File([new Uint8Array(6 * 1024 * 1024)], 'big.png', { type: 'image/png' })
      addImageFiles([bigFile])
      await flush()

      expect(onError).toHaveBeenCalledWith(expect.stringContaining('超过'))
      expect(pendingImages.value).toHaveLength(0)
    })

    it('合法图片读取为 data URL 加入待发送列表,可移除', async () => {
      const { addImageFiles, pendingImages, removePendingImage } = createPlayground(vi.fn())

      addImageFiles([new File(['img'], 'a.png', { type: 'image/png' })])
      await vi.waitFor(() => expect(pendingImages.value).toHaveLength(1))
      expect(pendingImages.value[0]).toBe('data:image/png;base64,aW1n')

      removePendingImage(0)
      expect(pendingImages.value).toHaveLength(0)
    })

    it('达到数量上限后拒绝继续添加', async () => {
      const onError = vi.fn()
      const { addImageFiles, pendingImages } = createPlayground(vi.fn(), onError)

      const file = () => new File(['img'], 'a.png', { type: 'image/png' })
      addImageFiles([file(), file(), file(), file(), file()])
      await vi.waitFor(() => expect(pendingImages.value).toHaveLength(5))
      // 第二批:已提交 5 张 + 读取中,第 5 张触发上限
      addImageFiles([file(), file(), file(), file(), file()])
      await vi.waitFor(() => expect(pendingImages.value).toHaveLength(9))
      // 已满 9 张:整批拒绝
      addImageFiles([file()])
      await flush()

      expect(onError).toHaveBeenCalledTimes(2)
      expect(pendingImages.value).toHaveLength(9)
    })
  })
})
