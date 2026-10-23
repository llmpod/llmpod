<script setup lang="ts">
import type { GlobalSearchItem, LayoutMenuGroup } from '@repo/naive-ui'
import { goToPortalLogin, useBrand } from '@repo/hooks'
import { useAuthStore } from '@/store/auth'
import { logout as logoutApi } from '@/api/auth'
import { useSearchActions } from './useSearchActions'
import { useSubTabs } from './useSubTabs'
import { allMenus } from './menus'

const auth = useAuthStore()
const { brandConfig } = useBrand()
const router = useRouter()

/** 菜单项是否展示:有权限(含公共页)且未被本人自助关闭(hasMenu 已含 enabled=false 判定);无权限或已关闭均直接隐藏 */
function menuVisible(name: string) {
  return auth.hasMenu(name)
}

/** 按 /auth/menus 下发过滤:无权限或被本人自助关闭均不渲染;空分组不展示 */
const menus = computed<LayoutMenuGroup[]>(() =>
  allMenus
    .map((group) => ({
      ...group,
      children: (group.children ?? []).filter((item) => menuVisible(item.name)),
    }))
    .filter((group) => (group.children ?? []).length > 0),
)

/** 用户名首字符作为头像文本 */
const avatarText = computed(() => auth.userInfo?.username?.charAt(0).toUpperCase() ?? '')

/** 路由 name → 菜单页面名:全局搜索操作项的行尾小字(所属页面) */
const pageLabelByName = new Map<string, string>()
for (const group of allMenus) {
  for (const item of group.children ?? []) pageLabelByName.set(item.name, item.label)
  if (group.name) pageLabelByName.set(group.name, group.label)
}

/** 全局搜索-操作分组:各页面 search.ts 自动聚合,按目标页面菜单权限过滤 */
const searchActions = useSearchActions((name) => pageLabelByName.get(name))

/** 路由 name → 所属菜单分组:子 tab 搜索项归入父页面所在分组 */
const groupOfName = computed(() => {
  const map = new Map<string, LayoutMenuGroup>()
  for (const group of menus.value) {
    const children = group.children ?? []
    if (!children.length) {
      if (group.name) map.set(group.name, group)
      continue
    }
    for (const item of children) map.set(item.name, group)
  }
  return map
})

/** 全局搜索-子 tab 项:各页面 tabs.ts 自动聚合,按父页面菜单权限过滤 */
const subTabItems = useSubTabs(
  (name) => {
    const group = groupOfName.value.get(name)
    return group ? { label: group.label, icon: group.icon } : undefined
  },
  (name) => pageLabelByName.get(name),
)

/** 全局搜索数据源:可见菜单页面(沿菜单分组展示,本人关闭/无权限页不进搜索) + 子 tab 页面 + 操作 */
const searchItems = computed<GlobalSearchItem[]>(() => {
  const pages = menus.value.flatMap((group) =>
    (group.children ?? []).map((item) => ({
      key: `page:${item.name}`,
      label: item.label,
      group: group.label,
      icon: group.icon,
      onSelect: () => void router.push({ name: item.name }),
    })),
  )
  return [...pages, ...subTabItems.value, ...searchActions.value]
})

/** 版本更新信息仅管理端展示(头像红点/检查更新/关于页),租户端不感知 */

const userOptions = [
  { label: '设置', key: 'config' },
  { label: '退出登录', key: 'logout' },
]

function handleUserSelect(key: string) {
  if (key === 'config') void router.push({ name: 'config' })
  if (key === 'logout') logout()
}

async function logout() {
  // 后端登出失败（如 token 已失效）不阻塞本地清理
  await logoutApi().catch(() => {})
  auth.clearToken()
  goToPortalLogin()
}
</script>

<template>
  <LmLayout
    title="租户后台"
    :menus="menus"
    :footer="brandConfig.footerText"
    collapsed-persist-key="llmpod:tenant:menu-collapsed"
    :menu-setting-to="auth.hasMenu('config') ? { name: 'config', query: { tab: 'menu' } } : undefined"
  >
    <!-- 全局搜索入口移至侧栏首个菜单项上方;折叠态由 slot 透出的 collapsed 收成方形图标按钮 -->
    <template #search="{ collapsed }">
      <LmGlobalSearch
        :items="searchItems"
        variant="bar"
        :collapsed="collapsed"
        recent-key="llmpod:tenant:search-recent"
      />
    </template>
    <template #header-extra>
      <n-dropdown :options="userOptions" trigger="click" @select="handleUserSelect">
        <div class="flex items-center gap-[.8rem] cursor-pointer">
          <n-avatar round size="small">{{ avatarText }}</n-avatar>
          <span>{{ auth.userInfo?.username }}</span>
        </div>
      </n-dropdown>
    </template>
  </LmLayout>
</template>
