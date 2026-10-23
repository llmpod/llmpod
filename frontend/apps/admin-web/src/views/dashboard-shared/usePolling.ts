/**
 * 可见性感知的静默轮询(三屏大屏共用):
 * - 可见时按 intervalMs 单链 setTimeout 轮询(同时只有一条链,refresh 会先清排期再拉取);
 * - 标签页隐藏暂停,回到前台立即拉一轮并恢复周期;
 * - 单次失败静默跳过,下个周期自动重试(错误提示由 HTTP 层统一处理);
 * - 首轮由调用方在 onMounted 主动 refresh()(便于控制首屏 loading);
 * - 卸载自动清理。
 * 必须在 setup 顶层调用(内部注册生命周期钩子)。
 */
export function usePolling(fetcher: () => Promise<unknown>, intervalMs: number) {
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false

  function clearTimer() {
    if (timer !== undefined) {
      clearTimeout(timer)
      timer = undefined
    }
  }

  function schedule() {
    if (disposed || document.hidden || timer !== undefined) return
    timer = setTimeout(() => void refresh(), intervalMs)
  }

  async function refresh() {
    if (disposed) return
    clearTimer()
    try {
      await fetcher()
    } catch {
      // 单次失败静默跳过,下个周期自动重试
    }
    schedule()
  }

  function onVisibilityChange() {
    if (document.hidden) clearTimer()
    else void refresh()
  }

  onMounted(() => {
    document.addEventListener('visibilitychange', onVisibilityChange)
  })
  onUnmounted(() => {
    disposed = true
    clearTimer()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return { refresh }
}
