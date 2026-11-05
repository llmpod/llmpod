import { NpsFeedbackDialog } from '@repo/naive-ui'
import { useBrand } from '../../brand/useBrand'
import { useDialog } from '../dialog/useDialog'
import type { DialogHandle } from '../dialog/types'

/** 提交时回传给调用方的数据 */
export interface NpsFeedbackPayload {
  /** 评分（1–5） */
  score: number
  /** 用户填写的建议或原因；未填写为 null */
  comment: string | null
}

/** 打开反馈弹窗的选项 */
export interface OpenNpsFeedbackOptions {
  /** 弹窗标题；默认用当前品牌名拼一句 NPS 问法 */
  title?: string
  /** 弹窗宽度（rem，1rem = 10px）；默认 44 = 440px */
  area?: number
  /**
   * 提交回调：上报等副作用由调用方负责。
   * 抛错时弹窗保留并提示「提交失败」，不关闭。
   */
  onSubmit?: (payload: NpsFeedbackPayload) => void | Promise<void>
}

/**
 * 打开 NPS 反馈弹窗（命令式）。
 *
 * 只负责弹窗本身：不判断触发时机、不做本地去重、不内置上报。
 * 什么时候弹、要不要记录已填过、往哪里上报，全部由调用方决定。
 *
 * @example
 * ```ts
 * openNpsFeedback({
 *   onSubmit: ({ score, comment }) => submitNpsFeedback({ score, comment }),
 * })
 * ```
 */
export function openNpsFeedback(options: OpenNpsFeedbackOptions = {}): DialogHandle {
  const { brandConfig } = useBrand()

  return useDialog({
    title: options.title ?? `把 ${brandConfig.value.platformName} 推荐给同事，你会打几分？`,
    component: NpsFeedbackDialog,
    area: options.area ?? 44,
    btn: { confirmText: '提交', cancelText: '稍后再说' },
    dialogProps: { maskClosable: false },
    props: {
      onSubmit: (score: number, comment: string) =>
        options.onSubmit?.({ score, comment: comment || null }),
    },
  })
}
