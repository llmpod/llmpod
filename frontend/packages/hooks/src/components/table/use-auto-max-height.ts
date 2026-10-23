import { onMounted, onUnmounted, ref } from 'vue'
import type { Ref } from 'vue'

/** 可用高度下限:极端小屏(可用高为负/过小)时保底,避免表格被压到不可用 */
const MIN_BODY_HEIGHT = 240

/**
 * 表格视口自适应最大高度:测量「表格到滚动容器可视区顶的偏移」与
 * 「表格底到滚动容器可视内容底之间的全部下方占位」,生成 calc(100vh - Npx),
 * 让表格恰好占满剩余可视区、页面不产生整体滚动。
 *
 * 下方占位分两段实测,无硬编码常量:
 * 1. 表格到滚动容器(不含)之间:逐级累加「父级内容盒底 - 当前子元素底」,
 *    覆盖分页行/批量行/卡片下 padding 等各级家具;
 * 2. 滚动容器内部:表格所在顶层子元素之后的所有在流内容(页脚及其 padding),
 *    从最靠上后续兄弟的顶部量到容器可视内容底。兄弟之前的空隙(如页脚 mt-auto
 *    弹性 margin)会被表格增高吸收,不计入,保证能填满。
 *
 * 所有偏移均换算为「距容器 border-box 顶 + scrollTop」的滚动不变量,页面已滚动时
 * 测量依然准确;取整用 ceil 宁短勿溢出,避免亚像素误差顶出滚动条。
 * 重算时机:挂载后 + 窗口 resize + 表格自身/页面根元素尺寸变化(ResizeObserver)。
 */
export function useAutoMaxHeight(rootRef: Ref<HTMLElement | null>) {
  const maxHeight = ref<string | undefined>(undefined)

  let resizeObserver: ResizeObserver | null = null
  let rafId = 0
  let observedPathEl: Element | null = null

  /** 从表格元素向上找最近的纵向滚动祖先(n-layout 的 scroll-container 等) */
  function findScrollAncestor(el: HTMLElement): HTMLElement | null {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const { overflowY } = window.getComputedStyle(node)
      if (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay') {
        return node as HTMLElement
      }
    }
    return null
  }

  /** 表头行实际高度:max-height 生效时表头独立于表体渲染,body 预算需扣除,否则恰好溢出一个表头 */
  function headerHeight(el: HTMLElement): number {
    const headerCell = el.querySelector<HTMLElement>('.n-data-table-th')
    const headerBox = el.querySelector<HTMLElement>('.n-data-table-base-table-header')
    return headerCell?.offsetHeight || headerBox?.offsetHeight || 0
  }

  /** 页面根(滚动容器直接子级)高度变化代表上方内容重排(统计卡渲染/字体加载),需跟踪 */
  function observePathEl(el: Element): void {
    if (observedPathEl === el) return
    if (observedPathEl) resizeObserver?.unobserve(observedPathEl)
    observedPathEl = el
    resizeObserver?.observe(el)
  }

  function compute(): void {
    const el = rootRef.value
    if (!el || !el.isConnected) return
    const scroller = findScrollAncestor(el)

    // 1) 表格底到滚动容器(不含)之间的下方占位:逐级累加「父级内容盒底 - 当前子元素底」
    let below = 0
    let child: HTMLElement = el
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      if (parent === scroller) break
      const style = window.getComputedStyle(parent)
      const contentBottom =
        parent.getBoundingClientRect().bottom -
        (parseFloat(style.paddingBottom) || 0) -
        (parseFloat(style.borderBottomWidth) || 0)
      below += contentBottom - child.getBoundingClientRect().bottom
      child = parent
    }

    let available: number
    if (scroller) {
      observePathEl(child)
      const scrollerRect = scroller.getBoundingClientRect()
      const scrollerStyle = window.getComputedStyle(scroller)
      // 容器可视内容底(距 border-box 顶,滚动不变量)
      const contentBottomInside =
        scroller.clientHeight -
        (parseFloat(scrollerStyle.borderBottomWidth) || 0) -
        (parseFloat(scrollerStyle.paddingBottom) || 0)
      const scrollTop = scroller.scrollTop

      // 2) 容器内、表格所在顶层子元素之后的所有下方占位:
      //    a. 顶层子元素自身的尾部 padding/margin(表格在它内部,尾部留白在表格之下);
      //    b. 其后所有在流兄弟(页脚等),从最靠上兄弟的顶部量到可视内容底。
      //       兄弟之前的空隙(如页脚 mt-auto 弹性 margin)会被表格增高吸收,不计入。
      const childStyle = window.getComputedStyle(child)
      let belowInScroller =
        (parseFloat(childStyle.paddingBottom) || 0) + (parseFloat(childStyle.marginBottom) || 0)
      let minSiblingTopInside = Number.POSITIVE_INFINITY
      for (let sib = child.nextElementSibling; sib; sib = sib.nextElementSibling) {
        const style = window.getComputedStyle(sib)
        if (style.position === 'absolute' || style.position === 'fixed') continue
        if (style.display === 'none') continue
        const sibTopInside = sib.getBoundingClientRect().top - scrollerRect.top + scrollTop
        if (sibTopInside < minSiblingTopInside) minSiblingTopInside = sibTopInside
      }
      if (minSiblingTopInside !== Number.POSITIVE_INFINITY) {
        belowInScroller += Math.max(0, contentBottomInside - minSiblingTopInside)
      }

      const offsetInside = el.getBoundingClientRect().top - scrollerRect.top + scrollTop
      available = contentBottomInside - offsetInside - below - belowInScroller
    } else {
      available = window.innerHeight - el.getBoundingClientRect().top - below
    }
    if (!Number.isFinite(available)) return

    const bodyHeight = Math.max(available - headerHeight(el), MIN_BODY_HEIGHT)
    maxHeight.value = `calc(100vh - ${Math.ceil(window.innerHeight + 20 - bodyHeight)}px)`
  }

  function schedule(): void {
    cancelAnimationFrame(rafId)
    rafId = window.requestAnimationFrame(compute)
  }

  onMounted(() => {
    window.addEventListener('resize', schedule)
    // 路由过渡(lm-page/lm-page-fly 含 transform)进行中 rect 被扭曲,过渡结束后须重测
    document.addEventListener('transitionend', schedule, true)
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(schedule)
      // 自身宽高变化(侧栏折叠换行、tab 显隐);文档高度兜底;
      // 页面根(滚动容器直接子级)在首次 compute 中经 observePathEl 补充观察
      if (rootRef.value) resizeObserver.observe(rootRef.value)
      resizeObserver.observe(document.documentElement)
    }
    // 首次测量须在 observer 创建之后:observePathEl 依赖 observer 已就绪
    compute()
  })

  onUnmounted(() => {
    window.removeEventListener('resize', schedule)
    document.removeEventListener('transitionend', schedule, true)
    resizeObserver?.disconnect()
    resizeObserver = null
    observedPathEl = null
    cancelAnimationFrame(rafId)
    rafId = 0
  })

  return { maxHeight }
}
