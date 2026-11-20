import { closeAllDialog, getActivePlatform, goToPlatform } from '@repo/hooks'
import type { Router } from 'vue-router'

export function setupGuard(router: Router) {
  // 与 admin/tenant 一致:命令式弹窗宿主挂在 App.vue,站内重定向(回 login)不会销毁它,
  // 路由切换后需要主动 settle 在途弹窗的 Promise。
  router.afterEach(() => {
    closeAllDialog()
  })

  router.beforeEach((to) => {
    // 登录页始终放行（允许已登录用户重新登录覆盖 token）
    if (to.name === 'login') return true

    const platform = getActivePlatform()
    if (!platform) return { name: 'login' }

    // 已登录 → 整页跳转到对应平台默认首页
    goToPlatform(platform)
    return false
  })
}
