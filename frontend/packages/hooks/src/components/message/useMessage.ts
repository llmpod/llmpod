import { reactive, ref, type VNode } from 'vue'
import { pushMessage, removeMessage } from './imperative'
import type { MessageHandle, UseMessageOptions } from './types'

/** 长文本阈值:字符串内容超过该长度时默认不自动关闭,显示关闭按钮供用户读完手动关闭 */
const LONG_CONTENT_THRESHOLD = 50

/** 命令式打开信息提示。调用即挂载并显示,返回控制器。
 * 默认 3000ms 后自动关闭;可通过 duration 覆盖,传 null 则不自动关闭。
 * 内容为字符串且超过 50 字时,未显式传 duration/closable 则默认不自动关闭
 * 并显示关闭按钮(显式传参以用户为准)。
 */
export function useMessage(options: UseMessageOptions): MessageHandle {
  const id = Symbol()
  let timer: ReturnType<typeof setTimeout> | null = null

  // 长文本默认不自动关闭且可手动关闭,避免内容未读完即消失
  const isLongContent =
    typeof options.content === 'string' && options.content.length > LONG_CONTENT_THRESHOLD

  const state = reactive({
    type: options.type ?? 'info',
    content: options.content,
    closable: options.closable ?? isLongContent,
    showIcon: options.showIcon ?? true,
    icon: options.icon as VNode | (() => VNode) | undefined,
  })

  // 用 ref 持有 state 引用,确保 reactive 在 reactive 化的对象上不被解包
  const stateRef = ref(state)

  const close = () => {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    removeMessage(id)
  }

  // 显式传参以用户为准(含 null 不自动关);未传时:长文本不自动关、短文本 3000ms
  const duration =
    options.duration !== undefined ? options.duration : isLongContent ? null : 3000
  if (duration !== null && duration > 0) {
    timer = setTimeout(close, duration)
  }

  pushMessage({
    id,
    state: stateRef.value,
    onClose: options.onClose,
  })

  return { close }
}
