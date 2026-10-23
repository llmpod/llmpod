import { onScopeDispose, ref } from 'vue'
import type { Ref } from 'vue'

/**
 * 移动端断点查询:≤767px。
 * 与 LmLayout 抽屉导航、UnoCSS md:(≥768)互补;调整断点须三处同步。
 */
export const MOBILE_QUERY = '(max-width: 767px)'

export interface UseBreakpointReturn {
  /** 是否处于移动端断点(≤767px) */
  isMobile: Ref<boolean>
}

/**
 * 响应式断点 Hook:窄屏判定统一入口。
 * 业务里判断断点一律用它(不各自 matchMedia);CSS 侧用 UnoCSS md:/lg: 前缀。
 * 无组件上下文的纯逻辑调用安全:仅跳过生命周期注册,不警告。
 */
export function useBreakpoint(): UseBreakpointReturn {
  const isMobile = ref(false)
  // SSR / 旧环境兜底:无 matchMedia 视为桌面
  if (typeof window === 'undefined' || !window.matchMedia) {
    return { isMobile }
  }
  const media = window.matchMedia(MOBILE_QUERY)
  isMobile.value = media.matches

  function onChange(event: MediaQueryListEvent): void {
    isMobile.value = event.matches
  }

  media.addEventListener('change', onChange)
  onScopeDispose(() => media.removeEventListener('change', onChange), true)
  return { isMobile }
}
