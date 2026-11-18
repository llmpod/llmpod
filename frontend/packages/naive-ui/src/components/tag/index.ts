import { h } from 'vue'
import type { VNode } from 'vue'
import { NTag } from 'naive-ui'
import type { TagProps } from 'naive-ui'

/** 渲染单行省略标签的可选项 */
export interface EllipsisTagOptions {
  /**
   * 标签最大宽度(rem,项目 1rem=10px)。
   * 实际取值会用 min(maxWidth, 100%) 兜底,父容器更窄时随父级收缩,不会溢出列/卡片。
   * @default 12
   */
  maxWidth?: number
  /** 标签配色 @default 'primary' */
  type?: TagProps['type']
  /** 标签尺寸 @default 'small' */
  size?: TagProps['size']
  /** 是否描边 @default false */
  bordered?: boolean
  /** 自定义配色(与 NTag 的 color 一致) */
  color?: TagProps['color']
  /** 列表渲染用的 vnode key(v-for 同款语义,可选) */
  key?: string | number
}

/**
 * 渲染「超长文本单行省略」的标签。
 *
 * 为什么需要它:直接用 `NTag` 包长文本会溢出。`n-tag` 是 `inline-flex` + `white-space:nowrap`,
 * 且 naive 会把内容再包一层 `span.n-tag__content`——该层是不收缩的 flex item
 * (`min-width:auto` + `overflow:visible`),于是即便给标签本身限宽,文字仍会冲出标签外;
 * 在标签内再套 `NEllipsis`/`truncate` 同样无效,因为内层宽度永远等于全文本宽。
 *
 * 做法:把标签自身改成块级文本容器(`display:block` + `width:fit-content`),
 * `n-tag__content` 退化为普通行内内容,`text-overflow:ellipsis` 直接对它生效;
 * 垂直居中由 `line-height: var(--n-height)` 接管。长文本省略、短文本按内容自适应不撑满。
 *
 * 样式全部内联(`packages/*` 不参与各 app 的 UnoCSS 扫描),颜色/尺寸仍走 naive 主题变量,
 * 明暗与主题色联动不丢。`title` 落在标签根元素上,悬浮可看全名。
 *
 * @param text 标签文案
 * @param options 宽度与配色
 * @returns NTag 的 VNode,可直接放进表格列 render
 */
export function renderEllipsisTag(
  text: string,
  options: EllipsisTagOptions = {},
): VNode {
  const {
    maxWidth = 12,
    type = 'primary',
    size = 'small',
    bordered = false,
    color,
    key,
  } = options

  return h(
    NTag,
    {
      type,
      size,
      bordered,
      ...(color ? { color } : {}),
      ...(key !== undefined ? { key } : {}),
      // 悬浮看全名;naive Tag 无 title prop,经 attrs 透传到根元素
      title: text,
      style: {
        display: 'block',
        width: 'fit-content',
        maxWidth: `min(${maxWidth}rem, 100%)`,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        lineHeight: 'var(--n-height)',
      },
    },
    { default: () => text },
  )
}
