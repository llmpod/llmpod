<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { NavigationFailureType, useRouter, type RouteLocationNormalized, type RouteComponent } from 'vue-router'

defineOptions({ name: 'LmRouteProgress' })

/**
 * 路由切换进度条:仅当目标路由含未加载懒组件(chunk 未就绪,导航需等待下载)时启动,
 * 延迟约 150ms 渲染避免快速导航闪条;afterEach/onError 统一收尾。
 * 控制逻辑挂 router 钩子 → 菜单/全局搜索/页内跳转等所有导航入口统一受益。
 * 由 LmLayout 挂载:仅登录后布局内出现(登录页/portal 不涉及)。
 */
const router = useRouter()

const visible = ref(false)
const percent = ref(0)

/** 延迟显示阈值(ms):chunk 在此窗口内就绪则进度条完全不出现 */
const SHOW_DELAY = 150
/** 显示阈值到冲线间的缓慢爬升间隔(ms) */
const TRICKLE_INTERVAL = 400

let showTimer: ReturnType<typeof setTimeout> | undefined
let hideTimer: ReturnType<typeof setTimeout> | undefined
let trickleTimer: ReturnType<typeof setInterval> | undefined

/** 目标路由是否含未加载懒组件(vue-router 加载后会写回组件对象,仍为函数即未加载) */
function hasLazyComponent(to: RouteLocationNormalized): boolean {
  return to.matched.some(record =>
    Object.values(record.components ?? {}).some(comp => typeof comp === 'function'),
  )
}

function clearTimers() {
  clearTimeout(showTimer)
  clearTimeout(hideTimer)
  showTimer = undefined
  hideTimer = undefined
  if (trickleTimer) {
    clearInterval(trickleTimer)
    trickleTimer = undefined
  }
}

/** 启动一次懒加载导航:先快速推进到 35%,再缓慢爬升(不到 100%,等真实完成冲线) */
function start() {
  clearTimers()
  percent.value = 0
  showTimer = setTimeout(() => {
    visible.value = true
    percent.value = 35
    trickleTimer = setInterval(() => {
      percent.value = Math.min(percent.value + 3, 90)
    }, TRICKLE_INTERVAL)
  }, SHOW_DELAY)
}

/** 导航结束(成功/失败):未显示过则静默收场;显示过则冲 100% 后淡出隐藏 */
function finish() {
  if (showTimer) {
    clearTimeout(showTimer)
    showTimer = undefined
  }
  if (trickleTimer) {
    clearInterval(trickleTimer)
    trickleTimer = undefined
  }
  if (!visible.value) return
  percent.value = 100
  hideTimer = setTimeout(() => {
    visible.value = false
    percent.value = 0
  }, 300)
}

const unregisterBefore = router.beforeEach((to) => {
  if (hasLazyComponent(to)) start()
})
const unregisterAfter = router.afterEach((_to, _from, failure) => {
  // 被取消的导航(被更新的导航取代)也会触发 afterEach:进度条归属新导航,不在此收尾,
  // 否则快速连点菜单时旧导航的 afterEach 会误杀新导航刚启动的进度条
  // (failure.type 类型标注与 enum 无类型重叠,转数值比较)
  if (failure && Number(failure.type) === NavigationFailureType.cancelled) return
  finish()
})
const unregisterError = router.onError(() => finish())

onBeforeUnmount(() => {
  unregisterBefore()
  unregisterAfter()
  unregisterError()
  clearTimers()
})
</script>

<template>
  <div
    class="lm-route-progress"
    :class="{ 'lm-route-progress--visible': visible }"
    :style="{ width: `${percent}%` }"
  />
</template>

<style scoped>
/* 顶部 fixed 细进度条:z-index 3000 在窄屏抽屉(2002)之上、消息弹窗之下 */
.lm-route-progress {
  position: fixed;
  top: 0;
  left: 0;
  z-index: 3000;
  height: 0.3rem;
  width: 0;
  border-radius: 0 0.3rem 0.3rem 0;
  background-color: var(--c-primary);
  opacity: 0;
  pointer-events: none;
  transition:
    width 0.25s ease,
    opacity 0.2s ease;
}

.lm-route-progress--visible {
  opacity: 1;
}
</style>
