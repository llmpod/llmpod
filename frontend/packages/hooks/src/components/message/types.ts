import type { VNode } from 'vue'

/** 信息提示类型 */
export type MessageType = 'info' | 'success' | 'warning' | 'error' | 'loading'

/** useMessage 入参 */
export interface UseMessageOptions {
  /** 提示类型,默认 info */
  type?: MessageType
  /** 提示文本 */
  content: string
  /** 自动关闭时长(ms);null 表示不自动关闭;默认 3000,内容超 50 字时默认 null */
  duration?: number | null
  /** 是否显示右侧关闭按钮,默认 false;内容超 50 字时默认 true */
  closable?: boolean
  /** 是否显示图标,默认 true */
  showIcon?: boolean
  /** 自定义图标,覆盖 type 默认图标 */
  icon?: VNode | (() => VNode)
  /** 关闭后回调 */
  onClose?: () => void
}

/** useMessage 返回的控制器 */
export interface MessageHandle {
  /** 关闭并销毁提示 */
  close: () => void
}

/**
 * 命令式信息提示队列项。
 * 与弹窗复用同一 imperative host 契约,由 MessageHost 渲染。
 */
export interface MessageInstance {
  /** 队列内唯一标识 */
  id: symbol
  /** 当前提示类型/内容等响应式状态,由 MessageHost 渲染时消费 */
  state: {
    type: MessageType
    content: string
    closable: boolean
    showIcon: boolean
    icon?: VNode | (() => VNode)
  }
  /** 关闭后回调 */
  onClose?: () => void
}
