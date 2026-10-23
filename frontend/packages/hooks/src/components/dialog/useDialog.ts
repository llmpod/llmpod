import {
  defineAsyncComponent,
  h,
  ref,
  shallowRef,
  type Component,
  type VNode,
} from 'vue'
import { NSpin } from 'naive-ui'
import { LmDialog } from '@repo/naive-ui'
import { pushDialog, removeDialog } from './imperative'
import type {
  DialogArea,
  DialogBtn,
  DialogBtnOptions,
  DialogComponent,
  DialogHandle,
  DialogTitle,
  UseDialogOptions,
} from './types'

/** 内部组件实例最小契约:可选暴露 onConfirm */
interface ExposedComponent {
  onConfirm?: (close: () => void) => unknown
}

/** 解析后的底部按钮配置 */
interface ResolvedBtn {
  show: boolean
  confirmText: string
  cancelText: string
  showConfirm: boolean
  showCancel: boolean
  loading: boolean
  disabled: boolean
}

const DEFAULT_BTN: ResolvedBtn = {
  show: true,
  confirmText: '确定',
  cancelText: '取消',
  showConfirm: true,
  showCancel: true,
  loading: false,
  disabled: false,
}

function resolveBtn(btn: DialogBtn | undefined): ResolvedBtn {
  if (btn === false) return { ...DEFAULT_BTN, show: false }
  if (btn === undefined || btn === true) return { ...DEFAULT_BTN }
  const opts = btn as DialogBtnOptions
  return {
    show: true,
    confirmText: opts.confirmText ?? DEFAULT_BTN.confirmText,
    cancelText: opts.cancelText ?? DEFAULT_BTN.cancelText,
    showConfirm: opts.showConfirm ?? true,
    showCancel: opts.showCancel ?? true,
    loading: opts.loading ?? false,
    disabled: opts.disabled ?? false,
  }
}

/** area → CSS style 对象。数字 n → `${n}rem`;字符串原样;[w,h] → 宽高 */
export function resolveArea(area: DialogArea | undefined): Record<string, string> {
  if (area === undefined) return {}
  const toCss = (value: number | string): string =>
    typeof value === 'number' ? `${value}rem` : value
  const [width, height] = Array.isArray(area) ? area : [area]
  const style: Record<string, string> = {}
  if (width !== undefined) style.width = toCss(width)
  if (height !== undefined) style.height = toCss(height)
  return style
}

/**
 * component 解析:函数视为动态导入,用 defineAsyncComponent 包裹并显示 NSpin;
 * 同步组件对象直接返回。
 */
function resolveComponent(component: DialogComponent | undefined): Component | undefined {
  if (component === undefined) return undefined
  if (typeof component === 'function') {
    return defineAsyncComponent({
      loader: async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 动态导入模块类型未知,由消费方保证
        const mod = await (component as () => Promise<any>)()
        return mod && typeof mod === 'object' && 'default' in mod ? mod.default : mod
      },
      loadingComponent: () => h(NSpin),
    })
  }
  return component
}

function isRenderableTitle(title: DialogTitle | undefined): title is VNode | (() => VNode) {
  return title !== undefined && typeof title !== 'string'
}

function renderTitle(title: VNode | (() => VNode)): VNode {
  return typeof title === 'function' ? (title as () => VNode)() : title
}

/**
 * 命令式打开弹窗。调用即挂载并显示,返回控制器。
 * 点"确定"或调用 handle.confirm() 触发:onConfirm 自治(组件自己调 close 决定关闭时机)。
 */
export function useDialog(options: UseDialogOptions): DialogHandle {
  const show = ref(true)
  const loading = ref(false)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 透传 props,类型由消费方决定
  const propsRef = ref<Record<string, any>>(options.props ? { ...options.props } : {})
  const componentRef = shallowRef<ExposedComponent | null>(null)

  const innerComponent = resolveComponent(options.component)
  const resolvedBtn = resolveBtn(options.btn)
  const areaStyle = resolveArea(options.area)
  const hasRenderableTitle = isRenderableTitle(options.title)

  const id = Symbol()

  const close = () => {
    removeDialog(id)
  }

  const confirm = async () => {
    if (loading.value) return
    const confirmFn = options.onConfirm ?? componentRef.value?.onConfirm
    if (!confirmFn) {
      close()
      return
    }
    loading.value = true
    try {
      await confirmFn(close)
    } finally {
      loading.value = false
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 透传 props,类型由消费方决定
  const setProps = (props: Record<string, any>) => {
    propsRef.value = { ...propsRef.value, ...props }
  }

  const renderDialog = (): VNode => {
    const slots: Record<string, () => VNode> = {}

    if (hasRenderableTitle && options.title !== undefined) {
      slots.header = () => renderTitle(options.title as VNode | (() => VNode))
    }

    if (innerComponent) {
      slots.default = () =>
        h(innerComponent as Component, {
          ref: componentRef,
          ...propsRef.value,
        })
    } else if (options.content) {
      const content = options.content
      slots.default = () =>
        typeof content === 'function' ? (content as () => VNode)() : content
    }

    return h(
      LmDialog,
      {
        show: show.value,
        title: typeof options.title === 'string' ? options.title : '',
        showFooter: resolvedBtn.show,
        showPositiveButton: resolvedBtn.showConfirm,
        showNegativeButton: resolvedBtn.showCancel,
        positiveText: resolvedBtn.confirmText,
        negativeText: resolvedBtn.cancelText,
        positiveLoading: loading.value || resolvedBtn.loading,
        positiveDisabled: resolvedBtn.disabled,
        style: areaStyle,
        'onUpdate:show': (value: boolean) => {
          show.value = value
          if (!value) close()
        },
        onPositiveClick: () => {
          void confirm()
        },
        onNegativeClick: () => {
          close()
        },
        onClose: () => {
          close()
        },
        ...(options.dialogProps ?? {}),
      },
      slots,
    )
  }

  pushDialog({ id, render: renderDialog, onClose: options.onClose })

  return { close, confirm, setProps }
}
