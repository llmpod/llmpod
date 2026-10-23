/* eslint-disable vue/one-component-per-file -- MessageHost 与其内部 MessageItem 是宿主-条目关系 */
import {
  defineComponent,
  h,
  onUnmounted,
  TransitionGroup,
  shallowRef,
  type ShallowRef,
} from 'vue'
import { LmMessage } from '@repo/naive-ui'
import type { MessageInstance } from './types'

const MESSAGE_GLOBAL_KEY = '__lm_message__'

interface MessageGlobalStore {
  /** 全局响应式队列:记录所有打开的命令式信息提示,由挂在 App 树内的 MessageHost 渲染 */
  queue: ShallowRef<MessageInstance[]>
  /** MessageHost 是否已挂载(仅用于 dev 警告) */
  mounted: boolean
}

/**
 * 命令式提示的全局状态,挂到 globalThis。
 * message 命令式 API 与 MessageHost 在 monorepo 循环依赖 / bundler 模块分裂下可能被实例化为
 * 不同模块记录,模块级变量会因此分裂成两份(MessageHost 写一份、message 读另一份),
 * 表现为「MessageHost 已挂载却报未挂载、提示不入队不显示」。
 * 挂到 globalThis 可保证二者始终读写同一份队列与挂载标志。
 */
const globalStore: MessageGlobalStore = (() => {
  const g = globalThis as unknown as Record<string, unknown>
  return (
    (g[MESSAGE_GLOBAL_KEY] as MessageGlobalStore | undefined) ??
    (g[MESSAGE_GLOBAL_KEY] = { queue: shallowRef<MessageInstance[]>([]), mounted: false })
  )
})()

const messageQueue = globalStore.queue

/** 仅 dev 警告用;源码包由各 app 的 Vite 编译,import.meta.env 由 Vite 注入,cast 以兼容库内 typecheck */
const isDev = (import.meta as unknown as { env?: { DEV?: boolean } }).env?.DEV ?? false

/** 压入一个提示实例并立即显示(由 MessageHost 渲染) */
export function pushMessage(instance: MessageInstance): symbol {
  if (isDev && !globalStore.mounted) {
    console.warn(
      '[useMessage] MessageHost 未挂载,提示将不可见。请在 App.vue 的 NaiveProvider 内放置 <MessageHost />。',
    )
  }
  messageQueue.value = [...messageQueue.value, instance]
  return instance.id
}

/** 从队列移除指定 id 的提示(幂等) */
export function removeMessage(id: symbol): void {
  const next = messageQueue.value
  const idx = next.findIndex(item => item.id === id)
  if (idx === -1) return
  const [removed] = next.splice(idx, 1)
  messageQueue.value = [...next]
  removed.onClose?.()
}

/** 一键关闭所有命令式提示 */
export function closeAllMessage(): void {
  const all = messageQueue.value
  messageQueue.value = []
  all.forEach(item => item.onClose?.())
}

/** 包裹单个提示:读响应式 state 渲染 LmMessage */
const MessageItem = defineComponent({
  name: 'LmMessageItem',
  props: {
    instance: { type: Object as () => MessageInstance, required: true },
  },
  setup(props) {
    return () =>
      h(LmMessage, {
        type: props.instance.state.type,
        content: props.instance.state.content,
        closable: props.instance.state.closable,
        showIcon: props.instance.state.showIcon,
        icon: props.instance.state.icon,
        onClose: () => removeMessage(props.instance.id),
      })
  },
})

/**
 * 命令式信息提示宿主。必须挂在应用根组件树内(置于 NaiveProvider 下),
 * 以继承主题/暗色、router、pinia 等全部上下文。
 * 无遮罩,提示在屏幕中央垂直堆叠,带进出场动画。
 */
export const MessageHost = defineComponent({
  name: 'LmMessageHost',
  setup() {
    globalStore.mounted = true
    onUnmounted(() => {
      globalStore.mounted = false
    })
    return () =>
      h(TransitionGroup, { name: 'lm-message', tag: 'div', class: 'lm-message-container' }, () =>
        messageQueue.value.map(item =>
          h(MessageItem, { key: item.id, instance: item }),
        ),
      )
  },
})
