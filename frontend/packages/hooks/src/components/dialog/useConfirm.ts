import { h, ref, type VNode } from 'vue'
import { NCheckbox, NInputNumber } from 'naive-ui'
import { LmDialog } from '@repo/naive-ui'
import { pushDialog, removeDialog } from './imperative'
import { resolveArea } from './useDialog'
import type { UseConfirmOptions } from './types'

/** useConfirm 取消时 reject 的错误;外部 try/catch 捕获以打断后续 await */
export class ConfirmCancelError extends Error {
  constructor(message = 'confirm cancelled') {
    super(message)
    this.name = 'ConfirmCancelError'
  }
}

interface CalcProblem {
  text: string
  result: number
}

function rand(min: number, max: number): number {
  return Math.floor(Math.random() * (max - min + 1)) + min
}

/** 生成随机加减法题目 */
function createCalcProblem(): CalcProblem {
  const a = rand(1, 9)
  const b = rand(1, 9)
  const op = Math.random() > 0.5 ? '+' : '-'
  const result = op === '+' ? a + b : a - b
  return { text: `${a} ${op} ${b} =`, result }
}

function renderNode(content: VNode | (() => VNode)): VNode {
  return typeof content === 'function' ? (content as () => VNode)() : content
}

/** 默认图标:warning 三角(内联 SVG,项目无图标库) */
function renderDefaultIcon(): VNode {
  return h(
    'svg',
    {
      viewBox: '0 0 1024 1024',
      width: '1.8em',
      height: '1.8em',
      'aria-hidden': true,
      style: 'color: #f0a020; flex-shrink: 0; margin-top: 2px;',
    },
    [
      h('path', {
        fill: 'currentColor',
        d: 'M549.2 138.6c-9.4-16.3-26.4-26.2-45.2-26.2s-35.8 9.9-45.2 26.2L91.2 832c-9.4 16.3-9.4 36.1 0 52.4s26.4 26.2 45.2 26.2h735.2c18.8 0 35.8-9.9 45.2-26.2s9.4-36.1 0-52.4L549.2 138.6zM504 768c-22.1 0-40-17.9-40-40s17.9-40 40-40 40 17.9 40 40-17.9 40-40 40zm40-168c0 22.1-17.9 40-40 40s-40-17.9-40-40V360c0-22.1 17.9-40 40-40s40 17.9 40 40v240z',
      }),
    ],
  )
}

/**
 * 命令式确认弹窗。确认 resolve(checkbox 模式返回勾选状态),取消/关闭 reject
 * (ConfirmCancelError),外部 try/catch 捕获以打断后续操作。
 */
export function useConfirm(options: UseConfirmOptions = {}): Promise<boolean | void> {
  return new Promise((resolve, reject) => {
    let settled = false

    const show = ref(true)
    const loading = ref(false)
    const checked = ref(
      typeof options.checkbox === 'object' ? (options.checkbox.defaultChecked ?? false) : false,
    )
    const calcInput = ref<number | null>(null)
    const calcProblem = options.calc ? createCalcProblem() : undefined

    const id = Symbol()

    function doConfirm(): void {
      if (loading.value || settled) return
      settled = true
      resolve(options.checkbox !== undefined ? checked.value : undefined)
      removeDialog(id)
    }

    function doCancel(): void {
      if (settled) return
      settled = true
      reject(new ConfirmCancelError())
      removeDialog(id)
    }

    function renderDialog(): VNode {
      const checkboxLabel =
        typeof options.checkbox === 'string' ? options.checkbox : options.checkbox?.label

      const slots: Record<string, () => VNode> = {}

      // 标题为 VNode/函数时走 header 插槽
      if (options.title !== undefined && typeof options.title !== 'string') {
        slots.header = () => renderNode(options.title as VNode | (() => VNode))
      }

      slots.default = () =>
        h('div', { style: 'display:flex;gap:1.2rem;align-items:flex-start;padding:0.4rem 0;' }, [
          options.icon ? renderNode(options.icon) : renderDefaultIcon(),
          h('div', { style: 'flex:1;min-width:0;' }, [
            options.content !== undefined
              ? typeof options.content === 'string'
                ? h('div', { style: 'line-height:1.6;' }, options.content)
                : renderNode(options.content)
              : null,
            calcProblem
              ? h('div', { style: 'margin-top:1rem;display:flex;align-items:center;gap:0.8rem;' }, [
                  h('span', { style: 'white-space:nowrap;' }, calcProblem.text),
                  h(NInputNumber, {
                    value: calcInput.value,
                    'onUpdate:value': (value: number | null) => {
                      calcInput.value = value
                    },
                    placeholder: '答案',
                    showButton: false,
                    style: 'width: 12rem;',
                  }),
                ])
              : null,
            checkboxLabel !== undefined
              ? h(
                  NCheckbox,
                  {
                    checked: checked.value,
                    'onUpdate:checked': (value: boolean) => {
                      checked.value = value
                    },
                  },
                  { default: () => checkboxLabel },
                )
              : null,
          ]),
        ])

      return h(
        LmDialog,
        {
          show: show.value,
          title: typeof options.title === 'string' ? options.title : '',
          showFooter: options.btn !== false,
          positiveText: options.confirmText ?? '确定',
          negativeText: options.cancelText ?? '取消',
          positiveLoading: loading.value,
          positiveDisabled: calcProblem ? calcInput.value !== calcProblem.result : false,
          style: resolveArea(options.area || 40),
          // 关闭 naive Modal 初始自动聚焦(默认会聚焦 DOM 中首个可聚焦元素,即"取消"按钮,
          // 导致回车误触取消);焦点留在触发元素,Tab 仍可进入弹窗(focus trap 不变)
          autoFocus: false,
          'onUpdate:show': (value: boolean) => {
            show.value = value
            if (!value) doCancel()
          },
          onPositiveClick: () => {
            doConfirm()
          },
          onNegativeClick: () => {
            doCancel()
          },
          onClose: () => {
            doCancel()
          },
        },
        slots,
      )
    }

    // onClose 兜底:closeAllDialog 清场(路由切换/登出)不经 onUpdate:show 流程,靠它 settle Promise 避免 await 永久挂起
    pushDialog({ id, render: renderDialog, onClose: () => doCancel() })
  })
}
