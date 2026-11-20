import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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

// ---------------------------------------------------------------------------
// 溢出检测:全页标签共用一个 ResizeObserver
// ---------------------------------------------------------------------------

/**
 * 元素 → 该标签的 measure 回调。
 * 授权模型/角色这类列表一行就有多个标签,单页可达数百个;每个标签各建一个 observer 时,
 * 窗口缩放、列显隐、卡片断点切换都会让这些回调各自排队。共用后回调逻辑不变,
 * 注册的观察对象只剩一个,观察成本从 O(标签数) 降到 O(1)。
 */
const tagMeasurers = new WeakMap<Element, () => void>()
let sharedObserver: ResizeObserver | null = null

/** 全页共享的超出检测目标;ResizeObserver 不可用(旧浏览器/测试环境)时静默跳过 */
function observeTag(el: HTMLElement, measure: () => void): void {
  if (typeof ResizeObserver === 'undefined') return
  tagMeasurers.set(el, measure)
  sharedObserver ??= new ResizeObserver((entries) => {
    for (const entry of entries) tagMeasurers.get(entry.target)?.()
  })
  sharedObserver.observe(el)
}

/** 注销观察:删除映射与 observe 目标(重复调用安全,标签卸载即可调) */
function unobserveTag(el: HTMLElement): void {
  tagMeasurers.delete(el)
  sharedObserver?.unobserve(el)
}

// ---------------------------------------------------------------------------
// 短文本快速路径:确定放得下就不挂 tooltip
// ---------------------------------------------------------------------------

/**
 * 估算宽度只用到宽度上限的这个比例以内时,判定「一定放得下」。
 *
 * 取 0.66 而不是 1:父容器可能比 maxWidth 更窄(`min(maxWidth, 100%)` 的分支,窄屏卡片常见),
 * 留三成余量让判定在这种场景下也不会误判。误判的代价只有一种——极窄容器里短标签悬浮
 * 不再提示;长文本(本功能的目标场景)一条都不会漏判。
 */
const SHORT_TEXT_RATIO = 0.66

/** n-tag 各尺寸的正文字号(px):small→fontSizeTiny、medium→fontSizeSmall、large→fontSizeMedium */
const TAG_FONT_PX: Record<string, number> = { tiny: 12, small: 12, medium: 14, large: 14 }

/** 全角字符(中日韩、全角标点):按 1em 计;其余按 0.6em 计(拉丁/数字的偏大估计) */
const WIDE_CHAR_RE
  = /[\u1100-\u115F\u2E80-\u303E\u3041-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uA000-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6]/

/** 估算文本渲染宽度(px):按字符类别累加 em 再乘字号 */
function estimateTextWidthPx(text: string, size: string): number {
  const fontPx = TAG_FONT_PX[size] ?? 14
  let em = 0
  for (const ch of text) em += WIDE_CHAR_RE.test(ch) ? 1 : 0.6
  return em * fontPx
}

/** 文本是否确定不会触发省略(宽度上限按 1rem=10px 折算) */
function fitsWithoutEllipsis(text: string, maxWidth: number, size: string): boolean {
  return estimateTextWidthPx(text, size) <= maxWidth * 10 * SHORT_TEXT_RATIO
}

/**
 * 标签样式:短路径与常规路径共用一份,保证两条分支的省略表现完全一致。
 * 样式全内联:packages/* 不参与消费方 app 的 UnoCSS 扫描,写类名不会被生成。
 */
function tagStyle(maxWidth: number): Record<string, string> {
  return {
    display: 'block',
    width: 'fit-content',
    maxWidth: `min(${maxWidth}rem, 100%)`,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    lineHeight: 'var(--n-height)',
  }
}

/** 标签本体 vnode(两条分支共用):props 与样式保持一致,差异只在外面要不要包 tooltip */
function renderTagVNode(
  props: {
    text: string
    maxWidth: number
    type: TagProps['type']
    size: TagProps['size']
    bordered: boolean
    color?: TagProps['color']
  },
  onRef?: (instance: unknown) => void,
): VNode {
  return h(
    NTag,
    {
      ...(onRef ? { ref: onRef } : {}),
      type: props.type,
      size: props.size,
      bordered: props.bordered,
      ...(props.color ? { color: props.color } : {}),
      style: tagStyle(props.maxWidth),
    },
    { default: () => props.text },
  )
}

/**
 * 单个标签:省略样式 + 仅在确实被省略时给 tooltip。
 *
 * 溢出检测走共享 ResizeObserver:列宽变化(列显隐、窗口缩放、卡片断点切换)后状态自动跟随,
 * 不必等下次 hover 才纠正;短文案不弹提示,避免"每个标签都顶个气泡"的噪音。
 *
 * 两条分支:
 * - 短文本(估算宽度远小于宽度上限):直接返回裸标签,不挂 NTooltip、不注册 observer ——
 *   省掉 popover/binder 那条实例链,这是密集标签列表里最大的一笔开销;
 * - 长文本:包 NTooltip,由 observer 判定是否真被省略(未省略时 tooltip disabled)。
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

    /** 按文本与宽度上限预估「一定放得下」:命中即走短文本快速路径 */
    const probablyFits = computed(() =>
      fitsWithoutEllipsis(props.text, props.maxWidth, String(props.size)),
    )

    function measure(): void {
      if (!rootEl) return
      // +1 抗亚像素:有的缩放比下 clientWidth 会向下取整
      truncated.value = rootEl.scrollWidth > rootEl.clientWidth + 1
    }

    /** 让观察状态跟随当前分支:短文本不观察,长文本才观察(文本/宽度变化时切换) */
    function syncObserver(): void {
      if (!rootEl) return
      if (probablyFits.value) unobserveTag(rootEl)
      else observeTag(rootEl, measure)
    }

    function setRootRef(instance: unknown): void {
      const el = (instance as WithRootEl | null)?.$el ?? null
      const next = el instanceof HTMLElement ? el : null
      // 分支切换会重建标签节点:先把旧节点的观察摘掉,避免 WeakMap 里留下悬空回调
      if (rootEl && rootEl !== next) unobserveTag(rootEl)
      rootEl = next
      if (!rootEl) return
      measure()
      syncObserver()
    }

    onMounted(() => {
      if (!rootEl) return
      measure()
      syncObserver()
    })

    // 文本或宽度上限变化可能翻转判定:重新测一次并同步观察状态
    watch(probablyFits, () => {
      if (!rootEl) return
      measure()
      syncObserver()
    })

    onBeforeUnmount(() => {
      if (rootEl) unobserveTag(rootEl)
      rootEl = null
    })

    return () => {
      const tag = renderTagVNode(props, setRootRef)
      // 短文本快速路径:确定放得下,连 tooltip 组件都不创建
      if (probablyFits.value) return tag
      return h(
        NTooltip,
        {
          trigger: 'hover',
          // 未溢出无需提示:短名标签保持干净
          disabled: !truncated.value,
        },
        {
          trigger: () => tag,
          default: () =>
            h(
              'div',
              { style: { maxWidth: '32rem', whiteSpace: 'normal', wordBreak: 'break-all' } },
              props.text,
            ),
        },
      )
    }
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
