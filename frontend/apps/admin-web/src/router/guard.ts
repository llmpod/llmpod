import { goToPortalLogin, message } from '@repo/hooks'
import { START_LOCATION, type Router } from 'vue-router'
import { QUICK_SETUP_DONE_KEY, QUICK_SETUP_PROMPT_KEY, QUICK_SETUP_SKIP_KEY } from '@/api/quick-setup'
import { useAuthStore } from '@/store/auth'

export function setupGuard(router: Router) {
  router.beforeEach(async (to, from) => {
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

    // 一键部署向导页不参与菜单鉴权(不在后端菜单内,由部署页空态等入口手动进入)
    if (to.name === 'quick-setup') return true

    // 重定向等无 name 路由直接放行
    if (!to.name) return true

    // 首次引导:服务端注入 first_guide=true 且本次为落地导航(from=START_LOCATION,非站内
    // 跳转)时写一次性握手标记,由 BasicLayout 挂载后弹「首次部署引导」介绍窗——不再强制
    // 改道 /quick-setup,落地停留在默认页,是否进入向导交给用户决定(守卫阶段 App 未挂载,
    // 直接 useDialog 会触发「DialogHost 未挂载」dev 警告,故经标记延迟到布局层弹)。
    // skip(稍后再说/离开向导)/done(已完成)为会话标记,抑制提示避免重复打扰。
    if (
      window.__LLMPOD_SETTINGS__?.first_guide === true &&
      from === START_LOCATION &&
      sessionStorage.getItem(QUICK_SETUP_SKIP_KEY) === null &&
      sessionStorage.getItem(QUICK_SETUP_DONE_KEY) === null
    ) {
      sessionStorage.setItem(QUICK_SETUP_PROMPT_KEY, '1')
    }

    // 一个菜单权限都没有 → 登录态已无意义,清 token 整页回登录页重新登录
    if (!Object.keys(auth.menus).length) {
      auth.clearToken()
      message.warning('账号无任何菜单权限,请重新登录')
      goToPortalLogin()
      return false
    }

    if (auth.hasMenu(String(to.name))) return true

    // 模块被本人自助关闭(如登录落地页已隐藏)→ 静默换到第一个可用菜单;无菜单权限才提示
    const selfHidden = auth.menuEntry(String(to.name))?.enabled === false
    const first = auth.firstMenuName()
    if (!selfHidden) message.error('无操作权限')
    return first && first !== to.name ? { name: first } : false
  })
}
