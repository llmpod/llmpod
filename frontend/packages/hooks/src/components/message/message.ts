import { useMessage } from './useMessage'
import type { MessageHandle, MessageType, UseMessageOptions } from './types'

type MessageContent = string

type MessageMethodOptions = Omit<UseMessageOptions, 'type' | 'content'>

function createMethod(type: MessageType) {
  return (content: MessageContent, options?: MessageMethodOptions): MessageHandle => {
    return useMessage({ type, content, ...options })
  }
}

/** 命令式信息提示便捷 API,等价调用 useMessage */
export const message = {
  info: createMethod('info'),
  success: createMethod('success'),
  warning: createMethod('warning'),
  error: createMethod('error'),
  loading: createMethod('loading'),
}
