<script setup lang="ts">
import type { GlobalSearchItem, LayoutMenuGroup } from '@repo/naive-ui'
import { goToPortalLogin, useBrand, useDialog, useVersionCheck } from '@repo/hooks'
import { useAuthStore } from '@/store/auth'
import { logout as logoutApi } from '@/api/auth'
import { checkUpdate } from '@/api/version'
import ChangePasswordDialog from '@/components/ChangePasswordDialog.vue'
import { maybeShowFirstGuide } from '@/components/firstGuide'
import { useSearchActions } from './useSearchActions'
import { useSubTabs } from './useSubTabs'
import { allMenus } from './menus'

const auth = useAuthStore()
const { brandConfig } = useBrand()
const router = useRouter()

const MENU_MIGRATION_KEY = 'llmpod:admin:menu-expanded:v1-migrated'

/** 菜单展开 key 从 group-{index} 迁移为分组稳定 key:旧 key 按索引映射新 key,然后整体重建缓存,避免新旧 key 混杂 */
function migrateExpandedKeys() {
  if (localStorage.getItem(MENU_MIGRATION_KEY) === '1') return
  try {
    const cached: unknown = JSON.parse(localStorage.getItem('llmpod:admin:menu-expanded') ?? 'null')
    if (Array.isArray(cached)) {
      const next = cached
        .filter((key): key is string => typeof key === 'string')
        .map((key) => (/^group-(\d+)$/.exec(key) ? allMenus[Number(key.slice(6))]?.key : key))
        .filter((key): key is string => typeof key === 'string')
      localStorage.setItem('llmpod:admin:menu-expanded', JSON.stringify([...new Set(next)]))
    }
  } catch {
    // 缓存损坏:交给 LmLayout 回退默认展开分组
  }
  localStorage.setItem(MENU_MIGRATION_KEY, '1')
}
migrateExpandedKeys()

/** 菜单项是否展示:有权限(含公共页)且未被本人自助关闭(hasMenu 已含 enabled=false 判定);无权限或已关闭均直接隐藏 */
function menuVisible(name: string) {
  return auth.hasMenu(name)
}

/** 按 /auth/menus 下发过滤:无权限或被本人自助关闭均不渲染;空分组不展示 */
const menus = computed<LayoutMenuGroup[]>(() =>
  allMenus
    .map((group): LayoutMenuGroup | null => {
      // 叶子菜单(无 children):按自身权限过滤,通过则原样保留
      if (!group.children?.length) {
        return group.name && menuVisible(group.name) ? group : null
      }
      // 分组菜单:按子项权限过滤,过滤后保持原 key 不变(稳定性不依赖索引)
      return {
        ...group,
        children: group.children.filter((item) => menuVisible(item.name)),
      }
    })
    .filter(
      (group): group is LayoutMenuGroup =>
        group !== null && (!group.children || group.children.length > 0),
    ),
)

/** 用户名首字符作为头像文本 */
const avatarText = computed(() => auth.userInfo?.username?.charAt(0).toUpperCase() ?? '')

/** 路由 name → 菜单页面名:全局搜索操作项与子 tab 的行尾小字(所属页面) */
const pageLabelByName = new Map<string, string>()
for (const group of allMenus) {
  for (const item of group.children ?? []) pageLabelByName.set(item.name, item.label)
  if (group.name) pageLabelByName.set(group.name, group.label)
}
// 已合并为某页面子 tab 的 redirect 路由(meta.searchGroup 指向归属页面):自动派生其归属页面名,无需逐个写死
for (const record of router.getRoutes()) {
  const owner = record.meta.searchGroup
  if (owner && typeof record.name === 'string') {
    const ownerLabel = pageLabelByName.get(owner)
    if (ownerLabel) pageLabelByName.set(record.name, ownerLabel)
  }
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

/** 全局搜索数据源:可见菜单页面(沿菜单分组展示,下线/无权限页不进搜索) + 子 tab 页面 + 操作 */
const searchItems = computed<GlobalSearchItem[]>(() => {
  const pages = menus.value.flatMap((group) => {
    // 叶子菜单(无 children):作为独立页面项,归入「页面」组
    if (!group.children?.length) {
      return group.name
        ? [
            {
              key: `page:${group.name}`,
              label: group.label,
              group: '页面',
              icon: group.icon,
              onSelect: () => void router.push({ name: group.name! }),
            },
          ]
        : []
    }
    return group.children
      .map((item) => ({
        key: `page:${item.name}`,
        label: item.label,
        group: group.label,
        icon: group.icon,
        onSelect: () => void router.push({ name: item.name }),
      }))
  })
  return [...pages, ...subTabItems.value, ...searchActions.value]
})

/** 版本更新检查：进入页面静默检查一次（异步，不阻塞首屏），红点由后端 has_update 判定 */
const { hasUpdate, silentCheck, manualCheck } = useVersionCheck({
  storageKey: 'llmpod:admin:update-viewed',
  fetchLatest: checkUpdate,
})
onMounted(() => {
  void silentCheck()
  // 首次部署引导:消费守卫握手标记,弹「一键部署」向导介绍窗(first_guide 落地时)
  maybeShowFirstGuide(router)
})

const userOptions = [
  { label: '设置', key: 'config' },
  { label: '修改密码', key: 'change-password' },
  { label: '检查更新', key: 'check-update' },
  { label: '退出登录', key: 'logout' },
]

/** 修改密码弹窗:自助改密(POST /auth/change-password),成功后 token 失效跳登录 */
function openChangePassword() {
  useDialog({
    title: '修改密码',
    component: ChangePasswordDialog,
    area: 36,
    btn: { confirmText: '确认修改', cancelText: '取消' },
    dialogProps: { maskClosable: false },
  })
}

function handleUserSelect(key: string) {
  if (key === 'config') void router.push({ name: 'config' })
  if (key === 'change-password') openChangePassword()
  if (key === 'check-update') void checkUpdateAndNavigate()
  if (key === 'logout') logout()
}

/** 头像菜单「检查更新」:发现新版本跳转关于页展示更新内容(红点已消),无更新仅 toast 提示 */
async function checkUpdateAndNavigate() {
  const info = await manualCheck()
  if (info?.has_update) void router.push({ name: 'about' })
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
    :title="brandConfig.platformName"
    :menus="menus"
    :footer="brandConfig.footerText"
    menu-persist-key="llmpod:admin:menu-expanded"
    collapsed-persist-key="llmpod:admin:menu-collapsed"
    :menu-setting-to="auth.hasMenu('config') ? { name: 'config', query: { tab: 'menu' } } : undefined"
    :fly-to-menu="{ 'quick-setup': 'deploy' }"
  >
    <!-- 全局搜索入口移至侧栏首个菜单项上方;折叠态由 slot 透出的 collapsed 收成方形图标按钮 -->
    <template #search="{ collapsed }">
      <LmGlobalSearch
        :items="searchItems"
        variant="bar"
        :collapsed="collapsed"
        recent-key="llmpod:admin:search-recent"
      />
    </template>
    <template #header-extra>
      <n-dropdown :options="userOptions" trigger="click" @select="handleUserSelect">
        <div class="flex items-center gap-[.8rem] cursor-pointer">
          <n-badge dot :show="hasUpdate">
            <n-avatar round size="small">{{ avatarText }}</n-avatar>
          </n-badge>
          <span>{{ auth.userInfo?.username }}</span>
        </div>
      </n-dropdown>
    </template>
  </LmLayout>
</template>
