import type { Component, InjectionKey, VNode } from 'vue'

/** 标题:字符串 | VNode | 渲染函数 */
export type DialogTitle = string | VNode | (() => VNode)

/** area 单值:数字(rem)或字符串(原样 CSS) */
export type DialogAreaValue = number | string

/** area:单值(仅宽)或 [宽, 高] */
export type DialogArea = DialogAreaValue | [DialogAreaValue, DialogAreaValue]

/** 内容组件:同步组件对象,或动态导入函数 */
export type DialogComponent =
  | Component
  | (() => Promise<Component | { default: Component }>)

/** btn 为对象时的字段 */
export interface DialogBtnOptions {
  confirmText?: string
  cancelText?: string
  showConfirm?: boolean
  showCancel?: boolean
  loading?: boolean
  disabled?: boolean
}

/** btn:false 隐藏底部;true 默认;对象自定义 */
export type DialogBtn = boolean | DialogBtnOptions

/** useDialog 入参 */
export interface UseDialogOptions {
  /** 标题:字符串走 title prop;VNode/函数走 #header 插槽 */
  title?: DialogTitle
  /** 内容组件;与 content 二选一,同时传入时 component 优先 */
  component?: DialogComponent
  /** 纯内容(VNode / 渲染函数),供 useConfirm 复用;与 component 二选一 */
  content?: VNode | (() => VNode)
  /** 宽高:数字 n → n rem;字符串原样;[w,h] → 宽高 */
  area?: DialogArea
  /** 底部按钮控制 */
  btn?: DialogBtn
  /** 传给内部组件的 props(响应式,可经 setProps 更新) */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 透传给内部组件,类型由消费方决定
  props?: Record<string, any>
  /** 透传给 LmDialog / NModal 的额外属性(maskClosable、style 等),调用方优先 */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 透传属性,类型由底层组件决定
  dialogProps?: Record<string, any>
  /** 覆盖默认确认逻辑;默认读取内部组件 expose 的 onConfirm */
  onConfirm?: (close: () => void) => void | Promise<void>
  /** 关闭后回调 */
  onClose?: () => void
}

/** useDialog 返回的控制器 */
export interface DialogHandle {
  /** 关闭并销毁弹窗 */
  close: () => void
  /** 编程式触发确认,等价点击"确定" */
  confirm: () => void | Promise<void>
  /** 动态更新内部组件 props(合并) */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 透传 props,类型由消费方决定
  setProps: (props: Record<string, any>) => void
}

/** useConfirm 入参 */
export interface UseConfirmOptions {
  title?: DialogTitle
  /** 右侧文字/内容:string | VNode | 渲染函数(jsx 自定义) */
  content?: string | VNode | (() => VNode)
  /** 自定义图标(覆盖默认 warning),VNode | 渲染函数 */
  icon?: VNode | (() => VNode)
  /** checkbox 模式:label 字符串或对象;确认时 resolve 返回勾选状态 */
  checkbox?: string | { label: string; defaultChecked?: boolean }
  /** calc 模式:随机加减法,答案正确才能点确认 */
  calc?: boolean
  /** 宽高(同 useDialog) */
  area?: DialogArea
  /** 底部按钮控制;false 隐藏 */
  btn?: DialogBtn
  /** 确认按钮文案,默认"确定" */
  confirmText?: string
  /** 取消按钮文案,默认"取消" */
  cancelText?: string
}

/**
 * 弹窗内 provide 的关闭方法 injection key。
 * 注:运行时导出(Symbol),非纯类型;供弹窗内后代组件 inject。
 */
export const DIALOG_CLOSE_KEY: InjectionKey<() => void> = Symbol('lm-dialog-close')

/** 命令式弹窗队列项:useDialog / useConfirm 共用的最小契约 */
export interface DialogInstance {
  /** 队列内唯一标识,兼作 v-for key */
  id: symbol
  /** 产出弹窗 VNode 的渲染函数;内部读取响应式状态以自动更新 */
  render: () => VNode
  /** 关闭后回调 */
  onClose?: () => void
}
