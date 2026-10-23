import OpenAI from 'openai'
import type { ChatCompletionMessageParam } from 'openai/resources/chat/completions'
import type { PlaygroundChatSender, PlaygroundMessage, PlaygroundParams } from './types'

export interface CreateChatSenderOptions {
  /**
   * OpenAI 兼容接口的 baseURL,例如 `/api/admin`。
   * 传函数时每次发送解析当前值(数据面端点动态加载场景),为空字符串时发送直接报错。
   */
  baseURL: string | (() => string)
  /**
   * 直连网关的 API Key;提供时由 SDK 自动携带 Authorization 头,优先于 getToken。
   * 传函数时每次发送解析当前值(用户切换 Key 场景),动态模式下解析为空时发送直接报错。
   */
  apiKey?: string | (() => string | undefined)
  /** 获取登录 token,每次发送时调用。 */
  getToken?: () => string | null
}

/** 解析「静态值或函数」选项。 */
function resolveOption<T>(option: T | (() => T)): T {
  return typeof option === 'function' ? (option as () => T)() : option
}

/**
 * 数据面根地址转 OpenAI SDK baseURL:去尾斜杠并补 `/v1` 前缀
 * (proxy-endpoint 返回的 base_url 不含版本段,接入文档地址 = base_url + /v1/...)。
 */
export function toOpenAIBaseURL(baseUrl: string): string {
  const trimmed = baseUrl.trim().replace(/\/+$/, '')
  return trimmed.endsWith('/v1') ? trimmed : `${trimmed}/v1`
}

/** DeepSeek/Qwen 等推理模型在 delta/message 上扩展的思考字段。 */
interface ReasoningCarrier {
  content?: string | null
  reasoning_content?: string | null
  reasoning?: string | null
}

/** 读取思考增量:优先 reasoning_content,兼容 reasoning 字段。 */
function pickReasoning(carrier: ReasoningCarrier | undefined): string {
  if (!carrier) return ''
  const reasoning = carrier.reasoning_content ?? carrier.reasoning
  return reasoning ?? ''
}

/** 网关在 200 响应/SSE 数据帧中返回的 OpenAI 标准错误体(one-api 类网关的渠道错误)。 */
interface GatewayErrorFrame {
  message?: string
  type?: string
  param?: string | null
  code?: string | null
}

/**
 * 网关错误帧异常:错误体挂在 error 字段,供 parse-error 提取原因与结构化字段;
 * 区别于普通流式失败,不触发非流式降级(重试同样会失败,徒增一次请求)。
 */
class GatewayError extends Error {
  readonly error: GatewayErrorFrame

  constructor(errorBody: GatewayErrorFrame) {
    super(
      typeof errorBody.message === 'string' && errorBody.message !== ''
        ? errorBody.message
        : '请求失败',
    )
    this.error = errorBody
  }
}

/** 组装发送给后端的消息列表:有 system prompt 时置于开头。 */
function buildRequestMessages(
  params: PlaygroundParams,
  messages: PlaygroundMessage[],
): ChatCompletionMessageParam[] {
  const result: ChatCompletionMessageParam[] = []
  if (params.systemPrompt.trim()) {
    result.push({ role: 'system', content: params.systemPrompt.trim() })
  }
  return result.concat(messages)
}

/** 组装 OpenAI 兼容请求体(stream 字段在调用处区分字面量类型)。 */
function buildRequestBody(params: PlaygroundParams, messages: PlaygroundMessage[]) {
  return {
    model: params.model,
    messages: buildRequestMessages(params, messages),
    max_tokens: params.maxTokens,
    // 温度/Top P:0 视为未设置,不传参走模型默认(与 reasoning_effort 的 default 哨兵同理)
    ...(params.temperature !== 0 ? { temperature: params.temperature } : {}),
    ...(params.topP !== 0 ? { top_p: params.topP } : {}),
    // 思考强度:default(默认)不传参,其余按 OpenAI reasoning_effort 档位发送
    ...(params.reasoningEffort === 'default'
      ? {}
      : { reasoning_effort: params.reasoningEffort }),
  }
}

/**
 * 创建 playground 发送函数:
 * - params.stream(默认 true)走 SSE 流式,失败时降级为非流式
 * - params.stream=false 直接非流式请求
 * 思考过程通过 onReasoning 回调(reasoning_content / reasoning 扩展字段),
 * 通过 dangerouslyAllowBrowser 在浏览器侧直连 OpenAI 兼容接口。
 */
export function createChatSender(options: CreateChatSenderOptions): PlaygroundChatSender {
  // 按当前解析值缓存的 client;baseURL/apiKey 变化(端点加载完成/用户切 Key)时重建
  let client: OpenAI | null = null
  let clientCacheKey = ''

  /** 解析当前配置并返回可用 client;动态选项缺失时抛出可读错误。 */
  function resolveClient(): { client: OpenAI; apiKey: string | undefined } {
    const baseURL = resolveOption(options.baseURL)
    if (!baseURL) throw new Error('数据面接入地址未加载,请稍后重试')

    const apiKey = resolveOption(options.apiKey)
    // 动态 Key 模式必须解析出有效 Key;静态模式保持旧缺省行为(placeholder,走 getToken 覆盖)
    if (typeof options.apiKey === 'function' && !apiKey) throw new Error('请先选择 API Key')

    const effectiveApiKey = apiKey ?? 'playground-placeholder'
    const cacheKey = `${baseURL}\n${effectiveApiKey}`
    if (!client || clientCacheKey !== cacheKey) {
      client = new OpenAI({
        baseURL,
        // SDK 要求 apiKey 非空;apiKey 模式由 SDK 默认头携带真实 key
        apiKey: effectiveApiKey,
        dangerouslyAllowBrowser: true,
        maxRetries: 0,
      })
      clientCacheKey = cacheKey
    }
    return { client, apiKey }
  }

  return async (params, messages, { onChunk, onReasoning, onRestart, signal }) => {
    const { client, apiKey } = resolveClient()
    // apiKey 模式用 SDK 默认 Authorization;登录态模式每次请求覆盖为真实 token
    const requestOptions = apiKey
      ? { signal }
      : { signal, headers: { Authorization: `Bearer ${options.getToken?.() ?? ''}` } }

    /** 非流式响应:正文与思考一次性回调。 */
    const consumeCompletion = (response: OpenAI.ChatCompletion) => {
      // 网关 200 响应也可能携带标准错误体:优先识别,避免 choices 缺失报难懂的 TypeError
      const gatewayError = (response as { error?: GatewayErrorFrame }).error
      if (gatewayError) throw new GatewayError(gatewayError)
      const message = response.choices[0]?.message as ReasoningCarrier | undefined
      const reasoning = pickReasoning(message)
      if (reasoning) onReasoning?.(reasoning)
      onChunk(message?.content ?? '')
    }

    // 显式非流式:直接请求,失败不降级(尊重用户选择)
    if (!params.stream) {
      const response = await client.chat.completions.create(
        { ...buildRequestBody(params, messages), stream: false },
        requestOptions,
      )
      consumeCompletion(response)
      return
    }

    try {
      const stream = await client.chat.completions.create(
        { ...buildRequestBody(params, messages), stream: true },
        requestOptions,
      )
      for await (const chunk of stream) {
        // SSE 数据帧携带标准错误体(渠道错误等):直接抛出,不降级非流式
        const gatewayError = (chunk as { error?: GatewayErrorFrame }).error
        if (gatewayError) throw new GatewayError(gatewayError)
        const delta = chunk.choices?.[0]?.delta as ReasoningCarrier | undefined
        const reasoning = pickReasoning(delta)
        if (reasoning) onReasoning?.(reasoning)
        const content = delta?.content
        if (content) onChunk(content)
      }
    } catch (error) {
      // 用户主动停止,直接结束
      if (signal.aborted) return
      // 网关错误帧已是明确错误:直接抛给界面渲染原因
      if (error instanceof GatewayError) throw error
      // 降级为非流式请求;再失败则抛给界面提示。
      // 降级前通知调用方重置累积缓冲:SSE 已消费的增量会被本轮完整响应替换,
      // 追加语义下不清缓冲会得到「半截回复 + 完整回复」的重复拼接。
      onRestart?.()
      try {
        const response = await client.chat.completions.create(
          { ...buildRequestBody(params, messages), stream: false },
          requestOptions,
        )
        consumeCompletion(response)
      } catch (fallbackError) {
        if (signal.aborted) return
        throw fallbackError
      }
    }
  }
}
