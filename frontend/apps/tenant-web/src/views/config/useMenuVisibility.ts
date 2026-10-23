import { ref } from 'vue'
import type { Component } from 'vue'
import { getMenus, updateMyMenus, type MenuItemData } from '@/api/auth'
import { MENU_PREFIX, PUBLIC_MENUS, useAuthStore } from '@/store/auth'
import { allMenus } from '@/layouts/menus'

/** 「模块管理」tab 行:本人可自助开关的侧栏菜单(仅展示层,不动功能权限) */
export interface MenuRow {
  /** 菜单路径(POST /auth/menus/update 提交的字段) */
  path: string
  /** 路由 name(侧栏声明 key) */
  name: string
  /** 菜单名(侧栏文案) */
  label: string
  /** 所属侧栏分组 label,设置页按它分区展示 */
  group: string
  /** 所属侧栏分组图标(与左侧菜单同源;未声明图标的组缺省) */
  groupIcon?: Component
  /** 当前是否显示(enabled=false=本人已关闭,侧栏直接隐藏) */
  enabled: boolean
  /** 锁定行(设置页自身):不可关闭,防止把自己锁在设置页外 */
  locked: boolean
}

/** 设置页路由 name:关掉它侧栏入口/底部齿轮/路由守卫都会失效,必须锁定 */
const SETTINGS_ROUTE = 'config'

/**
 * 字典 → 自助开关行:按侧栏分组声明遍历(分组/排序/文案与左侧菜单同源),
 * 组内叶子 ∩ /auth/menus 字典(key 存在=有权限)。
 * 公共页(不随字典显隐)不进列表;组内全被过滤时整组不出现。
 */
function buildRows(dict: Record<string, MenuItemData>): MenuRow[] {
  return allMenus.flatMap((group) => {
    // 有 children 时组内叶子即候选;无 children 的分组自身即叶子(与侧栏渲染口径一致)
    const leaves = group.children?.length
      ? group.children
      : group.name
        ? [{ label: group.label, name: group.name }]
        : []
    return leaves
      .filter((item) => !PUBLIC_MENUS.has(item.name))
      .flatMap((item) => {
        const entry = dict[`${MENU_PREFIX}${item.name}`]
        if (!entry) return []
        return [
          {
            path: `${MENU_PREFIX}${item.name}`,
            name: item.name,
            label: item.label,
            group: group.label,
            groupIcon: group.icon,
            enabled: entry.enabled !== false,
            locked: item.name === SETTINGS_ROUTE,
          },
        ]
      })
  })
}

/** 设置页「模块管理」tab:本人菜单显示自助开关(GET/POST /auth/menus),切换即全量覆盖保存;样式对齐管理端 */
export function useMenuVisibility() {
  const auth = useAuthStore()
  const loading = ref(false)
  /** 正在保存的行路径(null=空闲);同一时刻只跑一次保存,避免全量覆盖接口并发互踩 */
  const savingPath = ref<string | null>(null)
  const rows = ref<MenuRow[]>([])

  /** 用保存后的最终下线集回显行,并写回本地字典:侧栏即时生效 */
  function apply(disabled: string[]) {
    const off = new Set(disabled)
    rows.value = rows.value.map((row) => ({ ...row, enabled: !off.has(row.path) }))
    auth.applyDisabled(disabled)
  }

  async function load() {
    loading.value = true
    try {
      rows.value = buildRows((await getMenus()) ?? {})
    } finally {
      loading.value = false
    }
  }

  /** 单行开关:visible=false 表示本人关闭;切换即乐观更新并全量覆盖保存,失败回滚该行 */
  async function toggle(path: string, visible: boolean) {
    if (savingPath.value) return
    const row = rows.value.find((item) => item.path === path)
    if (!row || row.locked || row.enabled === visible) return
    const prev = row.enabled
    row.enabled = visible
    savingPath.value = path
    try {
      // disabled 即最终态:须提交当前全部处于关闭态的行
      const result = await updateMyMenus(
        rows.value.filter((item) => !item.enabled).map((item) => item.path),
      )
      apply(result.disabled)
    } catch {
      // 错误提示(422 非法路径等)由 http 层统一弹出;失败回滚该行,界面回到服务端态
      row.enabled = prev
    } finally {
      savingPath.value = null
    }
  }

  return { loading, savingPath, rows, load, toggle }
}
