import type {
  ChatCompletionContentPart,
  ChatCompletionContentPartImage,
  ChatCompletionContentPartText,
} from 'openai/resources/chat/completions'
import { reactive, ref } from 'vue'
import { parsePlaygroundError } from './parse-error'
import { splitReasoning } from './parse-reasoning'
import type {
  PlaygroundChatMessage,
  PlaygroundChatSender,
  PlaygroundMessage,
  PlaygroundModelOption,
  PlaygroundParams,
} from './types'

export interface UsePlaygroundOptions {
  sender: PlaygroundChatSender
  /** 本地操作错误(图片校验/读取失败等)的提示回调;AI 请求失败不走此回调,而是以 error 消息渲染到内容区。 */
  onError?: (message: string) => void
  /** 单张图片大小上限(MB),默认 5。 */
  maxImageMB?: number
  /** 待发送图片数量上限,默认 9。 */
  maxImageCount?: number
}

/** 采样参数默认值(重置用,不含 model/mode)。 */
export const PLAYGROUND_DEFAULT_SAMPLING = {
  stream: true,
  temperature: 0.7,
  maxTokens: 4096,
  topP: 0.9,
  systemPrompt: '',
  reasoningEffort: 'default',
} as const

/** 消息 id 自增序号。 */
let messageSeq = 0

/**
 * 切换模型时自动填充模型特有配置(其余参数保持用户当前值):
 * 思考强度:支持思考的模型填 default(不传参,模型自行决定),不支持填 none(显式关闭);未提供能力信息不动。
 */
export function applyModelDefaults(
  params: PlaygroundParams,
  model: PlaygroundModelOption | undefined,
): void {
  if (!model) return
  if (model.reasoning === true) params.reasoningEffort = 'default'
  if (model.reasoning === false) params.reasoningEffort = 'none'
}

/**
 * 创建响应式消息对象:必须以 reactive 代理形式持有,
 * 流式回调直接修改该对象才能逐帧触发视图更新(否则要等下一次渲染才一次性显示)。
 */
function createMessage(
  partial: Pick<PlaygroundChatMessage, 'role'> & Partial<PlaygroundChatMessage>,
): PlaygroundChatMessage {
  messageSeq += 1
  return reactive({
    id: messageSeq,
    content: '',
    reasoning: '',
    images: [],
    status: 'done',
    error: '',
    errorDetail: null,
    ...partial,
  })
}

/**
 * UI 消息转 OpenAI 请求消息:
 * - user 有图片时转 multipart(text + image_url data URL)
 * - assistant 只回传可见正文,思考过程不回传
 */
function toRequestMessage(message: PlaygroundChatMessage): PlaygroundMessage {
  if (message.role === 'assistant') {
    return { role: 'assistant', content: message.content }
  }
  if (message.images.length === 0) {
    return { role: 'user', content: message.content }
  }
  const parts: ChatCompletionContentPart[] = []
  if (message.content) {
    const textPart: ChatCompletionContentPartText = { type: 'text', text: message.content }
    parts.push(textPart)
  }
  for (const url of message.images) {
    const imagePart: ChatCompletionContentPartImage = { type: 'image_url', image_url: { url } }
    parts.push(imagePart)
  }
  return { role: 'user', content: parts }
}

/** 默认展示的示例对话:空会话/清空后回到这组消息。 */
function createDefaultMessages(): PlaygroundChatMessage[] {
  return [
    createMessage({ role: 'user', content: '你好' }),
    createMessage({ role: 'assistant', content: '你好，请问有什么可以帮助您的吗？' }),
  ]
}

export function usePlayground(options: UsePlaygroundOptions) {
  const maxImageBytes = (options.maxImageMB ?? 5) * 1024 * 1024
  const maxImageCount = options.maxImageCount ?? 9

  const params = reactive<PlaygroundParams>({
    model: '',
    mode: 'chat',
    ...PLAYGROUND_DEFAULT_SAMPLING,
  })

  const messages = ref<PlaygroundChatMessage[]>(createDefaultMessages())
  const input = ref('')
  /** 待发送图片(data URL)。 */
  const pendingImages = ref<string[]>([])
  const sending = ref(false)
  /** FileReader 读取中的图片数:数量上限校验需计入,避免异步窗口超发。 */
  let inflightImages = 0
  let abortController: AbortController | null = null

  /** 添加本地图片文件:校验类型/大小/数量后转 data URL。 */
  function addImageFiles(files: Iterable<File>) {
    for (const file of files) {
      if (!file.type.startsWith('image/')) {
        options.onError?.(`不支持的文件类型:${file.name}`)
        continue
      }
      if (file.size > maxImageBytes) {
        options.onError?.(`图片 ${file.name} 超过 ${options.maxImageMB ?? 5}MB 大小限制`)
        continue
      }
      if (pendingImages.value.length + inflightImages >= maxImageCount) {
        options.onError?.(`最多发送 ${maxImageCount} 张图片`)
        return
      }
      inflightImages += 1
      const reader = new FileReader()
      reader.onload = () => {
        inflightImages -= 1
        if (typeof reader.result === 'string') pendingImages.value.push(reader.result)
      }
      reader.onerror = () => {
        inflightImages -= 1
        options.onError?.(`读取图片 ${file.name} 失败`)
      }
      reader.readAsDataURL(file)
    }
  }

  /** 移除待发送图片。 */
  function removePendingImage(index: number) {
    pendingImages.value.splice(index, 1)
  }

  /** 内部:按 id 移除消息,无生成中挡板(供占位清理/公开删除复用)。 */
  function spliceMessage(message: PlaygroundChatMessage) {
    const index = messages.value.findIndex((item) => item.id === message.id)
    if (index >= 0) messages.value.splice(index, 1)
  }

  /** 删除单条消息;生成中禁止。 */
  function removeMessage(message: PlaygroundChatMessage) {
    if (sending.value) return
    spliceMessage(message)
  }

  /** 发送当前输入:构建 user 消息并请求生成。 */
  async function send() {
    const content = input.value.trim()
    const images = [...pendingImages.value]
    if ((!content && images.length === 0) || sending.value || !params.model) return

    messages.value.push(createMessage({ role: 'user', content, images }))
    input.value = ''
    pendingImages.value = []
    await generate()
  }

  /** 基于当前对话历史请求 assistant 回复,流式更新尾部占位消息。 */
  async function generate() {
    // 历史消息不含尾部 assistant 占位;空正文 assistant 消息(如纯错误消息)剔除,避免后端拒绝空 content
    const history = messages.value
      .map(toRequestMessage)
      .filter((msg) => msg.role !== 'assistant' || msg.content !== '')
    const assistantMessage = createMessage({ role: 'assistant', status: 'streaming' })
    messages.value.push(assistantMessage)
    sending.value = true
    abortController = new AbortController()

    // content/reasoning 原始累积(含 <think> 标签);每次增量后对整体重解析,幂等无状态
    let rawContent = ''
    let rawReasoning = ''

    const applyParse = () => {
      const { reasoning, visible } = splitReasoning(rawContent)
      assistantMessage.content = visible
      assistantMessage.reasoning = `${rawReasoning}${reasoning}`.trim()
    }

    const hasBody = () => assistantMessage.content !== '' || assistantMessage.reasoning !== ''

    try {
      await options.sender(params, history, {
        signal: abortController.signal,
        onChunk: (text) => {
          rawContent += text
          applyParse()
        },
        onReasoning: (text) => {
          rawReasoning += text
          applyParse()
        },
        onRestart: () => {
          // 发送器将重发请求(流式中断降级非流式):新一轮完整响应替换已消费增量,
          // 清空缓冲重解析,避免追加语义下的重复拼接
          rawContent = ''
          rawReasoning = ''
          applyParse()
        },
      })
      // 中断或后端返回空内容时,移除占位消息
      if (!hasBody()) {
        spliceMessage(assistantMessage)
      } else {
        assistantMessage.status = abortController.signal.aborted ? 'stopped' : 'done'
      }
    } catch (error) {
      if (abortController.signal.aborted) {
        // sender 契约外仍被中断:按停止处理
        if (!hasBody()) spliceMessage(assistantMessage)
        else assistantMessage.status = 'stopped'
      } else {
        // 请求失败:解析 OpenAI 标准错误,原因与 type/code/状态码渲染在消息气泡内(内容区)
        const { message: reason, detail } = parsePlaygroundError(error)
        assistantMessage.status = 'error'
        assistantMessage.error = reason
        assistantMessage.errorDetail = detail
      }
    } finally {
      sending.value = false
      abortController = null
    }
  }

  /** 编辑用户消息后重发:更新文本(图片保留),移除其后消息并重新生成。 */
  async function resendMessage(message: PlaygroundChatMessage, content: string) {
    if (sending.value || !params.model) return
    if (!content && message.images.length === 0) return
    const index = messages.value.findIndex((item) => item.id === message.id)
    if (index < 0 || message.role !== 'user') return
    message.content = content
    messages.value.splice(index + 1)
    await generate()
  }

  /** 重新生成指定 assistant 回复:移除该消息及其后消息,重新请求。 */
  async function regenerateMessage(message: PlaygroundChatMessage) {
    if (sending.value || !params.model) return
    const index = messages.value.findIndex((item) => item.id === message.id)
    if (index < 0 || message.role !== 'assistant') return
    messages.value.splice(index)
    await generate()
  }

  /** 停止当前生成。 */
  function stop() {
    abortController?.abort()
  }

  /** 清空对话与待发送图片,回到默认示例对话。 */
  function clear() {
    stop()
    messages.value = createDefaultMessages()
    pendingImages.value = []
  }

  return {
    params,
    messages,
    input,
    pendingImages,
    sending,
    send,
    stop,
    clear,
    addImageFiles,
    removePendingImage,
    removeMessage,
    resendMessage,
    regenerateMessage,
  }
}
