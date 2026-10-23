/* eslint-disable vue/one-component-per-file -- DialogHost 与其内部 DialogItem 是宿主-条目关系:每个队列项需独立组件实例,以 provide 各自的关闭方法(DIALOG_CLOSE_KEY) */
import { defineComponent, h, inject, onUnmounted, provide, shallowRef } from 'vue'
import { DIALOG_CLOSE_KEY, type DialogInstance } from './types'

/**
 * 全局响应式队列:记录所有打开的命令式弹窗,由挂在 App 树内的 DialogHost 渲染。
 * 弹窗组件实例因此挂在主应用组件树上,继承 router / pinia / i18n / app.provide 全部上下文,
 * DOM 仍由 LmDialog 内部的 NModal teleport 到 body。
 */
const dialogQueue = shallowRef<DialogInstance[]>([])

/** DialogHost 是否已挂载(仅用于 dev 警告,避免静默不可见) */
let hostMounted = false

/** 仅 dev 警告用;源码包由各 app 的 Vite 编译,import.meta.env 由 Vite 注入,cast 以兼容库内 typecheck */
const isDev = (import.meta as unknown as { env?: { DEV?: boolean } }).env?.DEV ?? false

/**
 * 压入一个弹窗实例并立即显示(由 DialogHost 渲染)。
 * 返回实例 id,供调用方后续 removeDialog 关闭。
 */
export function pushDialog(instance: DialogInstance): symbol {
  if (isDev && !hostMounted) {
    console.warn(
      '[useDialog] DialogHost 未挂载,弹窗将不可见。请在 App.vue 的 NaiveProvider 内放置 <DialogHost />。',
    )
  }
  dialogQueue.value = [...dialogQueue.value, instance]
  return instance.id
}

/** 从队列移除指定 id 的弹窗(幂等:不存在则跳过,不重复触发 onClose) */
export function removeDialog(id: symbol): void {
  const next = dialogQueue.value
  const idx = next.findIndex((item) => item.id === id)
  if (idx === -1) return
  const [removed] = next.splice(idx, 1)
  dialogQueue.value = [...next]
  removed.onClose?.()
}

/** 一键关闭所有命令式弹窗(路由切换/登出/错误兜底等清场场景) */
export function closeAllDialog(): void {
  const all = dialogQueue.value
  dialogQueue.value = [] // 先清空,避免 onClose 内副作用再次操作队列
  all.forEach((item) => item.onClose?.())
}

/** 组合式封装:弹窗内任意后代组件调用,拿到关闭本弹窗的方法 */
export function useDialogClose(): () => void {
  return inject(DIALOG_CLOSE_KEY, () => {})
}

/**
 * 包裹单个弹窗:在自身 setup 内 provide 关闭方法,为该弹窗建立独立 injection 作用域,
 * 使弹窗内后代组件经 useDialogClose() 关闭的是"自己所属"的弹窗。
 */
const DialogItem = defineComponent({
  name: 'LmDialogItem',
  props: {
    instance: { type: Object as () => DialogInstance, required: true },
  },
  setup(props) {
    provide(DIALOG_CLOSE_KEY, () => removeDialog(props.instance.id))
    return () => props.instance.render()
  },
})

/**
 * 命令式弹窗宿主。必须挂在应用根组件树内(置于 NaiveProvider 下),
 * 以继承主题/暗色、message、router、pinia 等全部上下文。
 */
export const DialogHost = defineComponent({
  name: 'LmDialogHost',
  setup() {
    hostMounted = true
    onUnmounted(() => {
      hostMounted = false
    })
    return () => dialogQueue.value.map((item) => h(DialogItem, { key: item.id, instance: item }))
  },
})
