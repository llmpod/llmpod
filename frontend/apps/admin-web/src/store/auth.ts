import { clearToken as clearPlatformToken, getToken, setToken as setPlatformToken } from '@repo/hooks'
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getMenus, getUserInfo, type MenuItemData, type UserInfoData } from '@/api/auth'
import { isRouteHidden } from '@/router/hidden'
import { allMenus } from '@/layouts/menus'

const PLATFORM = 'admin' as const
/** 菜单 code 前缀,与后端 access_domain 选域一致 */
export const MENU_PREFIX = '/admin/'

/** 公共页面:登录即可见,不依赖后端菜单权限(关于页等纯前端展示页) */
export const PUBLIC_MENUS = new Set(['about'])

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(getToken(PLATFORM))
  const userInfo = ref<UserInfoData | null>(null)
  /** 当前用户可见菜单:key=菜单路径,value=可见性标注;key 不存在=无功能权限,enabled=false=被本人自助关闭 */
  const menus = ref<Record<string, MenuItemData>>({})

  function setToken(value: string) {
    setPlatformToken(PLATFORM, value)
    token.value = value
  }

  function clearToken() {
    clearPlatformToken(PLATFORM)
    token.value = ''
    userInfo.value = null
    menus.value = {}
  }

  /** 拉取当前用户信息与可见菜单(登录后首次进入受保护页时调用) */
  let profilePromise: Promise<void> | null = null
  function fetchProfile(): Promise<void> {
    profilePromise ??= Promise.all([getUserInfo(), getMenus()])
      .then(([me, menuDict]) => {
        userInfo.value = me
        menus.value = menuDict ?? {}
      })
      .finally(() => {
        profilePromise = null
      })
    return profilePromise
  }

  /** 自助保存成功后按返回的最终下线集回写本地字典:侧栏/守卫立即生效,无需重新拉取 */
  function applyDisabled(disabled: string[]) {
    const off = new Set(disabled)
    menus.value = Object.fromEntries(
      Object.entries(menus.value).map(([path, entry]) => [path, { ...entry, enabled: !off.has(path) }]),
    )
  }

  /** 菜单原始标注:无权限(公共页/未下发)返回 undefined,被本人自助关闭返回 { enabled: false } */
  function menuEntry(name: string): MenuItemData | undefined {
    return menus.value[`${MENU_PREFIX}${name}`]
  }

  /** 路由 name 当前是否可用:有功能权限且未被本人自助关闭(公共页面放行;前端隐藏名单内的路由恒不可见,见 router/hidden.ts) */
  function hasMenu(name: string): boolean {
    if (isRouteHidden(name)) return false
    if (PUBLIC_MENUS.has(name)) return true
    const entry = menuEntry(name)
    return !!entry && entry.enabled !== false
  }

  /** 第一个可用(有权限、未被下线)且未被前端隐藏的菜单路由 name:按侧栏声明序取(与左侧菜单从上到下一致),无则返回空字符串 */
  function firstMenuName(): string {
    for (const group of allMenus) {
      // 有 children 时组内叶子即候选;无 children 的分组自身即叶子(与侧栏渲染口径一致)
      const leaves = group.children?.length
        ? group.children
        : group.name
          ? [{ name: group.name }]
          : []
      for (const item of leaves) {
        if (isRouteHidden(item.name)) continue
        const entry = menus.value[`${MENU_PREFIX}${item.name}`]
        if (entry && entry.enabled !== false) return item.name
      }
    }
    return ''
  }

  return {
    token,
    userInfo,
    menus,
    setToken,
    clearToken,
    fetchProfile,
    applyDisabled,
    menuEntry,
    hasMenu,
    firstMenuName,
  }
})
