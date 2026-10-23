import type { ChatCompletionMessageParam } from 'openai/resources/chat/completions'
import type { ReasoningEffort } from 'openai/resources/shared'

/** OpenAI 标准错误详情(响应体 { error: { message, type, param, code } } + HTTP 状态码)。 */
export interface PlaygroundErrorDetail {
  /** HTTP 状态码;SSE 错误帧/网络错误等场景缺省。 */
  status?: number
  /** 错误类型,如 invalid_request_error。 */
  type?: string
  /** 出错参数名。 */
  param?: string
  /** 错误码,如 model_not_found。 */
  code?: string
}

/**
 * UI 消息模型:组件内部展示用,发送时由 usePlayground 转换为 OpenAI 消息。
 */
export interface PlaygroundChatMessage {
  id: number
  role: 'user' | 'assistant'
  /** user:输入文本;assistant:可见正文(<think> 标签已剥离)。 */
  content: string
  /** assistant:思考过程(reasoning_content 字段与 <think> 标签解析合并)。 */
  reasoning: string
  /** user:待发送图片的 data URL 列表。 */
  images: string[]
  /** 消息状态:streaming 生成中 / done 完成 / stopped 手动停止 / error 失败。 */
  status: 'streaming' | 'done' | 'stopped' | 'error'
  /** status=error 时的错误原因主文案(优先 OpenAI 标准错误体的 error.message)。 */
  error: string
  /** status=error 时的 OpenAI 标准错误详情;本地错误/网络错误时为 null。 */
  errorDetail?: PlaygroundErrorDetail | null
}

/** 发送给后端的 OpenAI 格式消息(历史回传用)。 */
export type PlaygroundMessage = ChatCompletionMessageParam

/** 模型下拉选项。 */
export type PlaygroundModelOption = {
  label: string
  value: string
  /** 模型是否支持思考(对应路由能力标签「思考」);用于切换模型时填充思考强度。 */
  reasoning?: boolean
}

/** API Key 下拉选项。 */
export type PlaygroundApiKeyOption = {
  label: string
  value: string
}

/** playground 支持对话调试的模型类别(chat 接口可调用;embedding/rerank/图像/音频类排除)。 */
export const PLAYGROUND_CHAT_MODEL_TYPES: readonly string[] = ['llm', 'multimodal']

/**
 * 思考强度:'default' 为 UI 哨兵值,表示不传 reasoning_effort(由模型默认行为决定);
 * 其余档位即 OpenAI SDK 的 ReasoningEffort 枚举(none/minimal/low/medium/high/xhigh/max)。
 */
export type PlaygroundReasoningEffort = 'default' | NonNullable<ReasoningEffort>

/** 思考强度下拉选项:默认(不传参) + OpenAI reasoning_effort 各档位。 */
export const PLAYGROUND_REASONING_EFFORT_OPTIONS: {
  label: string
  value: PlaygroundReasoningEffort
}[] = [
  { label: '默认', value: 'default' },
  { label: '不思考 (none)', value: 'none' },
  { label: '极简 (minimal)', value: 'minimal' },
  { label: '低 (low)', value: 'low' },
  { label: '中 (medium)', value: 'medium' },
  { label: '高 (high)', value: 'high' },
  { label: '超高 (xhigh)', value: 'xhigh' },
  { label: '最大 (max)', value: 'max' },
]

/** 调试参数。 */
export interface PlaygroundParams {
  model: string
  mode: string
  /** 是否流式请求;true 走 SSE, false 一次性返回。 */
  stream: boolean
  /** 采样温度;0 视为未设置,请求时不传该参数(走模型默认)。 */
  temperature: number
  maxTokens: number
  /** 核采样阈值;0 视为未设置,请求时不传该参数(走模型默认)。 */
  topP: number
  systemPrompt: string
  /** 思考强度;'default' 不传参,其余按 OpenAI reasoning_effort 档位发送。 */
  reasoningEffort: PlaygroundReasoningEffort
}

/** 发送过程中的回调。 */
export interface PlaygroundChatCallbacks {
  /** 可见正文增量;非流式时一次性给出全文(可能含 <think> 标签)。 */
  onChunk: (text: string) => void
  /** 思考过程增量(reasoning_content / reasoning 字段)。 */
  onReasoning?: (text: string) => void
  /**
   * 发送器将重发请求(流式中断降级非流式等):此前经 onChunk/onReasoning
   * 累积的增量将被新一轮完整响应替换,调用方应先清空缓冲再继续累积,
   * 否则追加语义下会出现「半截回复 + 完整回复」的重复拼接。
   */
  onRestart?: () => void
  /** 中断信号,用于"停止生成"。 */
  signal: AbortSignal
}

/**
 * 发送函数,由 createChatSender 创建后通过 prop 注入组件。
 * 完成后 resolve,失败 reject(中断除外,中断时正常 resolve)。
 */
export type PlaygroundChatSender = (
  params: PlaygroundParams,
  messages: PlaygroundMessage[],
  callbacks: PlaygroundChatCallbacks,
) => Promise<void>
