import { defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import type { PropType, VNode } from 'vue'
import { NTag, NTooltip } from 'naive-ui'
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

/** 取组件实例的根 DOM(标签根节点即是省略/测量的容器) */
interface WithRootEl {
  $el?: unknown
}

/**
 * 单个标签:省略样式 + 仅在确实被省略时给 tooltip。
 *
 * 溢出检测走 ResizeObserver:列宽变化(列显隐、窗口缩放、卡片断点切换)后状态自动跟随,
 * 不必等下次 hover 才纠正;短文案不弹提示,避免"每个标签都顶个气泡"的噪音。
 */
const EllipsisTag = defineComponent({
  name: 'LmEllipsisTag',
  props: {
    text: { type: String, required: true },
    maxWidth: { type: Number, default: 12 },
    type: { type: String as PropType<TagProps['type']>, default: 'primary' },
    size: { type: String as PropType<TagProps['size']>, default: 'small' },
    bordered: { type: Boolean, default: false },
    color: { type: Object as PropType<TagProps['color']>, default: undefined },
  },
  setup(props) {
    /** 文本是否真的被省略(决定要不要挂 tooltip) */
    const truncated = ref(false)
    /** 标签根 DOM,用于宽度测量 */
    let rootEl: HTMLElement | null = null
    let observer: ResizeObserver | undefined

    function measure(): void {
      if (!rootEl) return
      // +1 抗亚像素:有的缩放比下 clientWidth 会向下取整
      truncated.value = rootEl.scrollWidth > rootEl.clientWidth + 1
    }

    function setRootRef(instance: unknown): void {
      const el = (instance as WithRootEl | null)?.$el ?? null
      rootEl = el instanceof HTMLElement ? el : null
      measure()
    }

    onMounted(() => {
      measure()
      if (rootEl && typeof ResizeObserver !== 'undefined') {
        observer = new ResizeObserver(measure)
        observer.observe(rootEl)
      }
    })

    onBeforeUnmount(() => {
      observer?.disconnect()
      observer = undefined
      rootEl = null
    })

    return () =>
      h(
        NTooltip,
        {
          trigger: 'hover',
          // 未溢出无需提示:短名标签保持干净
          disabled: !truncated.value,
        },
        {
          trigger: () =>
            h(
              NTag,
              {
                ref: setRootRef,
                type: props.type,
                size: props.size,
                bordered: props.bordered,
                ...(props.color ? { color: props.color } : {}),
                // 样式全内联:packages/* 不参与消费方 app 的 UnoCSS 扫描,写类名不会被生成
                style: {
                  display: 'block',
                  width: 'fit-content',
                  maxWidth: `min(${props.maxWidth}rem, 100%)`,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  lineHeight: 'var(--n-height)',
                },
              },
              { default: () => props.text },
            ),
          default: () =>
            h(
              'div',
              { style: { maxWidth: '32rem', whiteSpace: 'normal', wordBreak: 'break-all' } },
              props.text,
            ),
        },
      )
  },
})

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
 * 明暗与主题色联动不丢。悬浮提示用 `NTooltip`(非原生 title),且只在文本真被省略时出现。
 *
 * @param text 标签文案
 * @param options 宽度与配色
 * @returns 可渲染的 VNode,可直接放进表格列 render
 */
export function renderEllipsisTag(
  text: string,
  options: EllipsisTagOptions = {},
): VNode {
  const { maxWidth, type, size, bordered, color, key } = options

  return h(EllipsisTag, {
    ...(key !== undefined ? { key } : {}),
    text,
    ...(maxWidth !== undefined ? { maxWidth } : {}),
    ...(type !== undefined ? { type } : {}),
    ...(size !== undefined ? { size } : {}),
    ...(bordered !== undefined ? { bordered } : {}),
    ...(color !== undefined ? { color } : {}),
  })
}
