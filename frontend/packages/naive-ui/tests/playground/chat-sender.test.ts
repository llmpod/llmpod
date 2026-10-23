import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  constructorArgs: [] as Array<Record<string, unknown>>,
}))

vi.mock('openai', () => ({
  default: class MockOpenAI {
    chat = { completions: { create: mocks.create } }

    constructor(options: Record<string, unknown>) {
      mocks.constructorArgs.push(options)
    }
  },
}))

import { createChatSender, toOpenAIBaseURL } from '../../src'
import type { PlaygroundParams } from '../../src'

/** 构造最小对话参数。 */
function buildParams(overrides: Partial<PlaygroundParams> = {}): PlaygroundParams {
  return {
    model: 'glm-5.2',
    mode: 'chat',
    stream: true,
    temperature: 0.7,
    maxTokens: 128,
    topP: 0.9,
    systemPrompt: '',
    reasoningEffort: 'default',
    ...overrides,
  }
}

/** 构造流式响应的 async iterable。 */
function streamOf(chunks: unknown[]) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) yield chunk
    },
  }
}

beforeEach(() => {
  mocks.constructorArgs.length = 0
  vi.clearAllMocks()
})

describe('createChatSender', () => {
  it('apiKey 模式:SDK 以该 key 装配,请求不覆盖 Authorization 头', async () => {
    mocks.create.mockResolvedValue(
      streamOf([
        { choices: [{ delta: { content: '你' } }] },
        { choices: [{ delta: { content: '好' } }] },
      ]),
    )
    const sender = createChatSender({ baseURL: 'http://gw:3000/v1', apiKey: 'sk-gw' })

    const chunks: string[] = []
    await sender(buildParams({ systemPrompt: '你是助手' }), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: (text) => chunks.push(text),
    })

    expect(mocks.constructorArgs[0]).toMatchObject({
      baseURL: 'http://gw:3000/v1',
      apiKey: 'sk-gw',
      dangerouslyAllowBrowser: true,
    })
    expect(chunks).toEqual(['你', '好'])

    const [body, requestOptions] = mocks.create.mock.calls[0]
    expect(body).toMatchObject({ model: 'glm-5.2', stream: true })
    expect(body.messages[0]).toEqual({ role: 'system', content: '你是助手' })
    expect(requestOptions.headers).toBeUndefined()
  })

  it('getToken 模式:占位 apiKey 装配,每次请求覆盖 Authorization 为登录 token', async () => {
    mocks.create.mockResolvedValue(streamOf([{ choices: [{ delta: { content: 'hi' } }] }]))
    const sender = createChatSender({ baseURL: '/api/v1', getToken: () => 'tok-123' })

    await sender(buildParams(), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: () => {},
    })

    expect(mocks.constructorArgs[0]).toMatchObject({ apiKey: 'playground-placeholder' })
    const [, requestOptions] = mocks.create.mock.calls[0]
    expect(requestOptions.headers).toEqual({ Authorization: 'Bearer tok-123' })
  })

  it('流式 delta.reasoning_content 走 onReasoning,content 走 onChunk', async () => {
    mocks.create.mockResolvedValue(
      streamOf([
        { choices: [{ delta: { reasoning_content: '推理一' } }] },
        { choices: [{ delta: { reasoning_content: '推理二' } }] },
        { choices: [{ delta: { content: '答案' } }] },
      ]),
    )
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    const chunks: string[] = []
    const reasonings: string[] = []
    await sender(buildParams(), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: (text) => chunks.push(text),
      onReasoning: (text) => reasonings.push(text),
    })

    expect(reasonings).toEqual(['推理一', '推理二'])
    expect(chunks).toEqual(['答案'])
  })

  it('思考强度:档位值以 reasoning_effort 发送,默认(default)不传该参数', async () => {
    mocks.create.mockResolvedValue(streamOf([{ choices: [{ delta: { content: 'ok' } }] }]))
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    await sender(buildParams({ reasoningEffort: 'high' }), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: () => {},
    })
    expect(mocks.create.mock.calls[0][0].reasoning_effort).toBe('high')

    await sender(buildParams(), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: () => {},
    })
    expect('reasoning_effort' in mocks.create.mock.calls[1][0]).toBe(false)
  })

  it('采样参数:温度/Top P 为 0 视为未设置不传参,非 0 照传', async () => {
    mocks.create.mockResolvedValue(streamOf([{ choices: [{ delta: { content: 'ok' } }] }]))
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    // 非 0:正常携带
    await sender(buildParams({ temperature: 0.5, topP: 0.8 }), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: () => {},
    })
    const body = mocks.create.mock.calls[0][0]
    expect(body.temperature).toBe(0.5)
    expect(body.top_p).toBe(0.8)

    // 为 0:不传参,走模型默认
    await sender(buildParams({ temperature: 0, topP: 0 }), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: () => {},
    })
    const zeroBody = mocks.create.mock.calls[1][0]
    expect('temperature' in zeroBody).toBe(false)
    expect('top_p' in zeroBody).toBe(false)
  })

  it('stream=false:直接非流式请求,正文与思考一次性回调,失败不降级', async () => {
    mocks.create.mockRejectedValueOnce(new Error('bad request'))
    mocks.create.mockResolvedValueOnce({
      choices: [{ message: { content: '完整回答', reasoning_content: '完整思考' } }],
    })
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    // 先验证失败场景:直接抛出,不重试非流式
    await expect(
      sender(buildParams({ stream: false }), [{ role: 'user', content: 'hi' }], {
        signal: new AbortController().signal,
        onChunk: () => {},
      }),
    ).rejects.toThrow('bad request')
    expect(mocks.create).toHaveBeenCalledTimes(1)

    // 再验证成功场景
    const chunks: string[] = []
    const reasonings: string[] = []
    await sender(buildParams({ stream: false }), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: (text) => chunks.push(text),
      onReasoning: (text) => reasonings.push(text),
    })

    expect(mocks.create).toHaveBeenCalledTimes(2)
    const [body] = mocks.create.mock.calls[1]
    expect(body).toMatchObject({ stream: false })
    expect(reasonings).toEqual(['完整思考'])
    expect(chunks).toEqual(['完整回答'])
  })

  it('流式失败时降级非流式:响应的 reasoning_content 一次性走 onReasoning', async () => {
    mocks.create.mockRejectedValueOnce(new Error('sse unsupported'))
    mocks.create.mockResolvedValueOnce({
      choices: [{ message: { content: '降级回答', reasoning_content: '降级思考' } }],
    })
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    const chunks: string[] = []
    const reasonings: string[] = []
    await sender(buildParams(), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: (text) => chunks.push(text),
      onReasoning: (text) => reasonings.push(text),
    })

    expect(mocks.create).toHaveBeenCalledTimes(2)
    const [fallbackBody] = mocks.create.mock.calls[1]
    expect(fallbackBody).toMatchObject({ stream: false })
    expect(reasonings).toEqual(['降级思考'])
    expect(chunks).toEqual(['降级回答'])
  })

  it('流式中途失败降级:onRestart 先于降级响应回调(UR-006:调用方可清空已消费增量)', async () => {
    // SSE 已消费 1 帧后连接中断,catch 内降级非流式
    mocks.create.mockImplementationOnce(async () => ({
      async *[Symbol.asyncIterator]() {
        yield { choices: [{ delta: { content: '半截' } }] }
        throw new Error('sse broken')
      },
    }))
    mocks.create.mockResolvedValueOnce({
      choices: [{ message: { content: '完整回答' } }],
    })
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    const events: string[] = []
    await sender(buildParams(), [{ role: 'user', content: 'hi' }], {
      signal: new AbortController().signal,
      onChunk: (text) => events.push(`chunk:${text}`),
      onRestart: () => events.push('restart'),
    })

    // 顺序契约:半截增量 → restart(清缓冲) → 完整全文
    expect(events).toEqual(['chunk:半截', 'restart', 'chunk:完整回答'])
    expect(mocks.create).toHaveBeenCalledTimes(2)
    expect(mocks.create.mock.calls[1][0]).toMatchObject({ stream: false })
  })

  it('SSE 数据帧携带标准错误体:抛出错误原因,不降级非流式', async () => {
    // one-api 类网关以 HTTP 200 + data: {"error": {...}} 返回渠道错误
    mocks.create.mockResolvedValueOnce(
      streamOf([
        { choices: [{ delta: { content: '部分' } }] },
        { error: { message: '当前分组下无可用渠道', type: 'new_api_error' } },
      ]),
    )
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    await expect(
      sender(buildParams(), [{ role: 'user', content: 'hi' }], {
        signal: new AbortController().signal,
        onChunk: () => {},
      }),
    ).rejects.toThrow('当前分组下无可用渠道')
    expect(mocks.create).toHaveBeenCalledTimes(1)
  })

  it('非流式 200 响应携带标准错误体:抛出错误原因', async () => {
    mocks.create.mockResolvedValueOnce({
      error: { message: '余额不足', type: 'insufficient_quota', code: 'insufficient_quota' },
    })
    const sender = createChatSender({ baseURL: '/api/v1', apiKey: 'sk' })

    await expect(
      sender(buildParams({ stream: false }), [{ role: 'user', content: 'hi' }], {
        signal: new AbortController().signal,
        onChunk: () => {},
      }),
    ).rejects.toThrow('余额不足')
    expect(mocks.create).toHaveBeenCalledTimes(1)
  })

  it('动态模式:baseURL/apiKey 按发送时刻解析,配置变化时重建 client', async () => {
    mocks.create.mockResolvedValue(streamOf([{ choices: [{ delta: { content: 'hi' } }] }]))
    let currentBaseURL = ''
    let currentApiKey = ''
    const sender = createChatSender({
      baseURL: () => currentBaseURL,
      apiKey: () => currentApiKey,
    })

    const sendOnce = () =>
      sender(buildParams(), [{ role: 'user', content: 'hi' }], {
        signal: new AbortController().signal,
        onChunk: () => {},
      })

    // 端点加载完成:装配 client
    currentBaseURL = 'http://gw:3000/v1'
    currentApiKey = 'sk-1'
    await sendOnce()
    expect(mocks.constructorArgs).toHaveLength(1)
    expect(mocks.constructorArgs[0]).toMatchObject({
      baseURL: 'http://gw:3000/v1',
      apiKey: 'sk-1',
    })

    // 用户切换 Key:重建 client 携带新 Key
    currentApiKey = 'sk-2'
    await sendOnce()
    expect(mocks.constructorArgs).toHaveLength(2)
    expect(mocks.constructorArgs[1]).toMatchObject({ baseURL: 'http://gw:3000/v1', apiKey: 'sk-2' })

    // 配置未变化:复用缓存的 client
    await sendOnce()
    expect(mocks.constructorArgs).toHaveLength(2)
  })

  it('动态模式:端点未加载或 Key 未选中时抛可读错误,不发请求', async () => {
    const senderWithoutEndpoint = createChatSender({
      baseURL: () => '',
      apiKey: () => 'sk-1',
    })
    await expect(
      senderWithoutEndpoint(buildParams(), [{ role: 'user', content: 'hi' }], {
        signal: new AbortController().signal,
        onChunk: () => {},
      }),
    ).rejects.toThrow('数据面接入地址未加载')

    const senderWithoutKey = createChatSender({
      baseURL: () => 'http://gw:3000/v1',
      apiKey: () => undefined,
    })
    await expect(
      senderWithoutKey(buildParams(), [{ role: 'user', content: 'hi' }], {
        signal: new AbortController().signal,
        onChunk: () => {},
      }),
    ).rejects.toThrow('请先选择 API Key')

    expect(mocks.create).not.toHaveBeenCalled()
    expect(mocks.constructorArgs).toHaveLength(0)
  })
})

describe('toOpenAIBaseURL', () => {
  it('数据面根地址补 /v1:裸基址/尾斜杠/已带版本段均归一', () => {
    expect(toOpenAIBaseURL('http://192.0.2.100:3000')).toBe('http://192.0.2.100:3000/v1')
    expect(toOpenAIBaseURL('http://192.0.2.100:3000/')).toBe('http://192.0.2.100:3000/v1')
    expect(toOpenAIBaseURL('http://192.0.2.100:3000/v1/')).toBe('http://192.0.2.100:3000/v1')
    expect(toOpenAIBaseURL(' http://gw:3000 ')).toBe('http://gw:3000/v1')
  })
})
