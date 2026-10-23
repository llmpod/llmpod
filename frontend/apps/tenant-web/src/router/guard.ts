import { goToPortalLogin, message } from '@repo/hooks'
import type { Router } from 'vue-router'
import { useAuthStore } from '@/store/auth'

export function setupGuard(router: Router) {
  router.beforeEach(async (to) => {
    const auth = useAuthStore()
    const isLogin = !!auth.token

    if (to.meta.public) return true

    // 受保护页：未登录 → 整页跳 portal 登录页（带当前页 redirect）
    if (!isLogin) {
      goToPortalLogin()
      return false
    }

    // 预取目标页组件 chunk:与下方 fetchProfile 网络往返并行,消除
    // 「profile 接口返回才触发 BasicLayout/页面 chunk 下载」的串行等待
    if (!auth.userInfo) {
      const prefetch = (to.matched ?? []).map((record) => record.components?.default).filter(Boolean)
      void Promise.allSettled(prefetch.map((load) => (load as () => Promise<unknown>)()))
    }

    // 首次进入拉取用户信息与可见菜单（401 时由请求层清 token 并跳登录）
    if (!auth.userInfo) await auth.fetchProfile()

    // 重定向等无 name 路由直接放行
    if (!to.name) return true

    // 一个菜单权限都没有 → 登录态已无意义,清 token 整页回登录页重新登录
    if (!Object.keys(auth.menus).length) {
      auth.clearToken()
      message.warning('账号无任何菜单权限,请重新登录')
      goToPortalLogin()
      return false
    }

    if (auth.hasMenu(String(to.name))) return true

    // 菜单被本人自助关闭(如登录落地页已隐藏)→ 静默换到第一个可用菜单;无菜单权限才提示
    const selfHidden = auth.menuEntry(String(to.name))?.enabled === false
    const first = auth.firstMenuName()
    if (!selfHidden) message.error('无操作权限')
    return first && first !== to.name ? { name: first } : false
  })
}
