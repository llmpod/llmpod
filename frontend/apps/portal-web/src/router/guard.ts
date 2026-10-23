import { getActivePlatform, goToPlatform } from '@repo/hooks'
import type { Router } from 'vue-router'

export function setupGuard(router: Router) {
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
