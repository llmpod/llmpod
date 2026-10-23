<script setup lang="ts">
import type { MenuOption } from 'naive-ui'
import type { RouteLocationRaw } from 'vue-router'
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter, type RouteComponent } from 'vue-router'
import { NButton, NIcon, NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NMenu } from 'naive-ui'
import { MenuOutline, MoonOutline, ReorderThreeOutline, SettingsOutline, SunnyOutline } from '@vicons/ionicons5'
import { useBreakpoint, useTheme } from '@repo/hooks'
import LmLogo from './LmLogo.vue'
import LmFooterText from './LmFooterText.vue'
import LmRouteProgress from './LmRouteProgress.vue'
import type { LayoutMenuGroup } from './types'

defineOptions({ name: 'LmLayout' })

interface Props {
  /** 左上角产品名 */
  title?: string
  /** 分组菜单配置 */
  menus?: LayoutMenuGroup[]
  /** 侧边栏折叠(v-model) */
  collapsed?: boolean
  /** 页脚文案,空字符串不显示页脚 */
  footer?: string
  /** 菜单展开状态持久化的 localStorage key,不传则不持久化;同 origin 多端部署时须带平台前缀 */
  menuPersistKey?: string
  /** 折叠状态持久化的 localStorage key,不传则不持久化;同 origin 多端部署时须带平台前缀 */
  collapsedPersistKey?: string
  /** 离开指定路由时页面缩小飞入对应菜单项(源路由 name → 目标菜单 key),提示用户可从该菜单重新进入 */
  flyToMenu?: Record<string, string>
  /** 侧栏底部「菜单显示设置」入口的跳转目标(一般指向设置页菜单子 tab,如 { name: 'config', query: { tab: 'menu' } });不传则不显示入口 */
  menuSettingTo?: RouteLocationRaw
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  menus: () => [],
  collapsed: false,
  footer: '',
  menuPersistKey: '',
  collapsedPersistKey: '',
  flyToMenu: () => ({}),
  menuSettingTo: undefined,
})

const emit = defineEmits<{
  'update:collapsed': [value: boolean]
}>()

const route = useRoute()
const router = useRouter()

// 主题一键切换:仅在明暗之间切换,不改变 auto 以外的持久化策略
const { darkMode, setThemeMode } = useTheme()

// 优先用 View Transitions 从点击位置圆形展开,不支持的浏览器直接切换
function handleToggleTheme() {
  const nextMode = darkMode.value ? 'light' : 'dark'
  if (!document.startViewTransition) {
    setThemeMode(nextMode)
    return
  }
  const transition = document.startViewTransition(async () => {
    setThemeMode(nextMode)
    // 等 Vue 将新主题应用到 DOM 后再捕获新视图
    await nextTick()
  })
  void transition.ready.then(() => {
    // 斜切扫屏：45° 斜边从左上向右下扫过
    document.documentElement.animate(
      {
        clipPath: [
          'polygon(0% 0%, 0% 0%, -100% 100%, -100% 100%)',
          'polygon(0% 0%, 200% 0%, 100% 100%, 0% 100%)',
        ],
      },
      { duration: 400, easing: 'ease-out', pseudoElement: '::view-transition-new(root)' },
    )
  })
}

/** 菜单可见性由调用方驱动:/auth/menus 下发的权限过滤与开关置灰(disabled)都在调用方完成 */
const menuOptions = computed<MenuOption[]>(() =>
  props.menus.map((group, groupIndex) => {
    const icon = group.icon ? () => h(NIcon, null, { default: () => h(group.icon!) }) : undefined
    // 无 children:渲染为叶子菜单项,key 取 name 以支持导航与高亮
    if (!group.children?.length) {
      return {
        label: group.label,
        key: group.name ?? `group-${groupIndex}`,
        icon,
        disabled: group.disabled === true,
      }
    }
    // 分组 key 优先取 group.key;缺省退化为按序索引(与 defaultExpandedKeys 一致)
    return {
      label: group.label,
      key: group.key ?? `group-${groupIndex}`,
      icon,
      children: group.children.map((item) => ({
        label: item.label,
        key: item.name,
        icon: item.icon ? () => h(NIcon, null, { default: () => h(item.icon!) }) : undefined,
        disabled: item.disabled === true,
      })),
    }
  }),
)

// 默认展开分组:defaultExpanded 为 false 时初始折叠(叶子菜单无展开 key)
const defaultExpandedKeys = computed(() =>
  props.menus.flatMap((group, groupIndex) =>
    group.children?.length && group.defaultExpanded !== false
      ? [group.key ?? `group-${groupIndex}`]
      : [],
  ),
)

// 当前路由 name 作为选中 key;不在 menus 中时 n-menu 自然无高亮。
// 乐观高亮:点击菜单瞬间先写入目标 key(不等懒 chunk 下载),路由 resolve 后回写真实值
const activeKey = ref(String(route.name ?? ''))
watch(
  () => route.name,
  (name) => {
    activeKey.value = String(name ?? '')
  },
  { immediate: true },
)

// 菜单展开状态(受控):优先读本地缓存,无缓存或缓存损坏时回退默认展开分组
const expandedKeys = ref<string[]>(readCachedExpandedKeys() ?? defaultExpandedKeys.value)

function readCachedExpandedKeys(): string[] | null {
  if (!props.menuPersistKey) return null
  try {
    const cached: unknown = JSON.parse(localStorage.getItem(props.menuPersistKey) ?? 'null')
    return Array.isArray(cached) ? cached.map(String) : null
  } catch {
    return null
  }
}

function setExpandedKeys(keys: Array<string | number>) {
  expandedKeys.value = keys.map(String)
  if (props.menuPersistKey) {
    localStorage.setItem(props.menuPersistKey, JSON.stringify(expandedKeys.value))
  }
}

// 折叠状态(内部自管):优先读本地缓存,无缓存或缓存损坏时回退 prop 初始值;切换时持久化并对外 emit
const collapsedState = ref(readCachedCollapsed() ?? props.collapsed)

// 窄屏(≤767px):侧栏转为悬浮抽屉(transform 模式,不挤压内容区),与桌面折叠状态互不干扰;
// 断点判定统一走 useBreakpoint
const { isMobile } = useBreakpoint()
const drawerOpen = ref(false)

// 回到桌面断点时收起抽屉,避免残留悬浮侧栏
watch(isMobile, (mobile) => {
  if (!mobile) drawerOpen.value = false
})

/** 渲染用有效折叠态:窄屏抽屉展开时视为未折叠(菜单/标题/底部按钮随之切换形态) */
const siderCollapsed = computed(() => (isMobile.value ? !drawerOpen.value : collapsedState.value))

function readCachedCollapsed(): boolean | null {
  if (!props.collapsedPersistKey) return null
  try {
    const cached: unknown = JSON.parse(localStorage.getItem(props.collapsedPersistKey) ?? 'null')
    return typeof cached === 'boolean' ? cached : null
  } catch {
    return null
  }
}

function toggleCollapsed() {
  // 窄屏抽屉模式:底部按钮切换抽屉开合,不污染桌面折叠持久化状态
  if (isMobile.value) {
    drawerOpen.value = !drawerOpen.value
    return
  }
  collapsedState.value = !collapsedState.value
  if (props.collapsedPersistKey) {
    localStorage.setItem(props.collapsedPersistKey, String(collapsedState.value))
  }
  emit('update:collapsed', collapsedState.value)
}

/** sider 折叠事件(仅外部 trigger 触发;窄屏联动抽屉开合) */
function onSiderCollapsedChange(value: boolean) {
  if (isMobile.value) {
    drawerOpen.value = value
    return
  }
  collapsedState.value = value
  if (props.collapsedPersistKey) {
    localStorage.setItem(props.collapsedPersistKey, String(value))
  }
  emit('update:collapsed', value)
}

/** 菜单显示设置入口:跳转设置页菜单子 tab(平台开关/按成员开关在各自设置页内管理) */
function openMenuSetting() {
  if (props.menuSettingTo) void router.push(props.menuSettingTo)
}

// 当前路由所在分组自动展开:路由切换时把目标分组并入展开集合,用户在当前路由内手动折叠不强制还原
const activeGroupKey = computed(() => {
  const group = menuOptions.value.find((option) =>
    option.children?.some((item) => String(item.key) === activeKey.value),
  )
  return group ? String(group.key) : ''
})

watch(
  activeGroupKey,
  (key) => {
    if (key && !expandedKeys.value.includes(key)) {
      setExpandedKeys([...expandedKeys.value, key])
    }
  },
  { immediate: true },
)

function handleSelect(key: string) {
  // 乐观高亮:点击瞬间即切换选中态;导航失败/被拦截由 finally 拉回真实值
  activeKey.value = key
  void router
    .push({ name: key })
    .catch(() => undefined)
    .finally(() => {
      activeKey.value = String(route.name ?? '')
    })
  // 窄屏抽屉:选中菜单跳转后自动收起
  if (isMobile.value) drawerOpen.value = false
}

/**
 * 空闲预取全部路由懒 chunk:首次点击 ≈ 二次点击零延迟。
 * 串行加载 + 空闲触发,慢网下不与用户当前请求抢带宽;
 * 结果写回路由记录(与 vue-router 懒加载写回行为一致),后续导航与进度条判断均视作已加载。
 */
function prefetchRouteChunks() {
  void (async () => {
    for (const record of router.getRoutes()) {
      const components = record.components
      if (!components) continue
      for (const [name, comp] of Object.entries(components)) {
        if (typeof comp !== 'function') continue
        try {
          // 函数式组件与懒加载器同为函数,统一按懒加载器调用(路由表只允许 lazy,无函数式组件)
          const resolved = await (comp as () => Promise<unknown>)()
          // 动态 import 的 module 取 default(对齐 vue-router interop 行为)
          const component =
            resolved && typeof resolved === 'object' && 'default' in resolved
              ? (resolved as { default: RouteComponent }).default
              : (resolved as RouteComponent)
          components[name] = component
        } catch {
          // 预取失败静默:真正导航时 vue-router 自行重试,错误仍走现有提示链路
        }
      }
    }
  })()
}

onMounted(() => {
  // 空闲期触发:优先 requestIdleCallback(浏览器真正空闲),不支持时延迟兜底
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(() => prefetchRouteChunks())
  } else {
    setTimeout(prefetchRouteChunks, 500)
  }
})

// 缩小飞入菜单动画:从 flyToMenu 声明的源路由离开时,页面缩小飞入目标菜单项
const pageTransition = ref('lm-page')
let pendingFlyKey = ''

const unregisterFlyGuard = router.beforeEach((to, from) => {
  const targetKey = from.name ? props.flyToMenu[String(from.name)] : undefined
  pendingFlyKey = targetKey ?? ''
  pageTransition.value = targetKey ? 'lm-page-fly' : 'lm-page'
})
onBeforeUnmount(unregisterFlyGuard)

/** 给菜单项根节点打 data-menu-key,供飞入动画定位目标菜单项 */
function menuNodeProps(option: MenuOption) {
  return { 'data-menu-key': String(option.key ?? '') }
}

/** 定位飞入动画的目标菜单项:折叠态下子项不在 DOM 中,回退为其所属分组(顶层节点始终存在) */
function findFlyTarget(): HTMLElement | null {
  if (!pendingFlyKey) return null
  const direct = document.querySelector<HTMLElement>(`[data-menu-key="${pendingFlyKey}"]`)
  if (direct) return direct
  const parent = menuOptions.value.find((option) =>
    option.children?.some((item) => String(item.key) === pendingFlyKey),
  )
  return parent
    ? document.querySelector<HTMLElement>(`[data-menu-key="${String(parent.key ?? '')}"]`)
    : null
}

/** 离场前:脱离滚动容器裁剪(fixed 定位保持视觉位置不变),并把目标菜单项中心设为吸入原点 */
function handleBeforeLeave(el: Element) {
  if (!pendingFlyKey) return
  const htmlEl = el as HTMLElement
  const fromRect = htmlEl.getBoundingClientRect()
  htmlEl.style.position = 'fixed'
  htmlEl.style.left = `${fromRect.left}px`
  htmlEl.style.top = `${fromRect.top}px`
  htmlEl.style.width = `${fromRect.width}px`
  htmlEl.style.height = `${fromRect.height}px`
  htmlEl.style.margin = '0'
  htmlEl.style.zIndex = '100'
  htmlEl.style.pointerEvents = 'none'
  // 提前提升到合成层,吸入时 scale/opacity 只走合成器,避免逐帧重绘掉帧
  htmlEl.style.willChange = 'transform, opacity'
  const toRect = findFlyTarget()?.querySelector<HTMLElement>('.n-menu-item-content')
    ?.getBoundingClientRect()
  // 目标菜单项不存在或不可见时回退为原地吸入(CSS 变量取默认值 center)
  if (!toRect || toRect.width === 0 || toRect.height === 0) return
  // transform-origin 直接定在菜单项中心(可在元素自身之外),scale → 0 即被吸入该点
  const originX = toRect.left + toRect.width / 2 - fromRect.left
  const originY = toRect.top + toRect.height / 2 - fromRect.top
  htmlEl.style.setProperty('--lm-fly-origin', `${originX}px ${originY}px`)
}

/** 离场结束:闪烁目标菜单项,提示用户以后可从这里重新打开 */
function handleAfterLeave() {
  if (!pendingFlyKey) return
  const target = findFlyTarget()
  pendingFlyKey = ''
  if (!target) return
  target.classList.add('lm-menu-item-flash')
  setTimeout(() => target.classList.remove('lm-menu-item-flash'), 1700)
}
</script>

<template>
  <!-- 路由进度条:仅懒 chunk 未就绪的导航显示,挂在最外层避免随内容区滚动 -->
  <LmRouteProgress />
  <n-layout has-sider class="h-screen">
    <!-- 窄屏抽屉遮罩:点击关闭侧栏 -->
    <div
      v-if="isMobile && drawerOpen"
      class="lm-layout-mask"
      @click="drawerOpen = false"
    />
    <n-layout-sider
      :collapsed="siderCollapsed"
      :width="212"
      :collapsed-width="56"
      collapse-mode="width"
      :native-scrollbar="false"
      :class="isMobile ? 'lm-layout-sider--drawer bg-fill-1' : 'bg-fill-1'"
      :style="isMobile ? { transform: drawerOpen ? 'translateX(0)' : 'translateX(-100%)' } : undefined"
      @update:collapsed="onSiderCollapsedChange"
    >
      <!-- min-h-screen 撑满视口:菜单过短时把收起按钮顶到侧栏最底 -->
      <div class="min-h-screen flex flex-col">
        <!-- sticky 固定头部:菜单项过多滚动时 logo 区域不随菜单滚动;
             折叠态收起左右内边距,让标识方块在 56px 窄栏内居中 -->
        <div
          class="h-[6.4rem] flex items-center overflow-hidden sticky top-0 z-10 bg-fill-1"
          :class="siderCollapsed ? 'justify-center' : 'gap-[1.2rem] px-[1.6rem]'"
        >
          <slot name="logo">
            <!-- 承托块:主色软底圆角方块,对齐设计稿的品牌标识形态;shrink-0 防超长标题压缩 -->
            <span
              class="w-[3rem] h-[3rem] rounded-[1rem] bg-primary-soft text-primary flex items-center justify-center shrink-0"
            >
              <LmLogo :size="18" />
            </span>
            <!-- min-w-0 + truncate:平台名可能非常长,溢出时省略号截断,title 悬浮看全名 -->
            <span
              v-if="!siderCollapsed"
              :title="title"
              class="text-2xl text-strong font-semibold whitespace-nowrap min-w-0 truncate"
            >
              {{ title }}
            </span>
          </slot>
        </div>
        <!-- 全局搜索入口:由调用方注入(通常为 LmGlobalSearch),置于标识区与菜单之间;
             折叠态由 slot 透出的 collapsed 驱动形态切换,组件内不额外维护状态 -->
        <div
          v-if="$slots.search"
          class="shrink-0 px-[1rem] pb-[.8rem]"
        >
          <slot
            name="search"
            :collapsed="siderCollapsed"
          />
        </div>
        <n-menu
          class="lm-sider-menu"
          :options="menuOptions"
          :value="activeKey"
          :expanded-keys="expandedKeys"
          :collapsed="siderCollapsed"
          :collapsed-width="56"
          :indent="18"
          :root-indent="17"
          :icon-size="16"
          :collapsed-icon-size="16"
          :node-props="menuNodeProps"
          @update:value="handleSelect"
          @update:expanded-keys="setExpandedKeys"
        />
        <!-- sticky 固定底部:菜单过短时 mt-auto 贴底,过长滚动时按钮钉在可视区底部;折叠态 56px 宽放不下并排按钮,改竖排 -->
        <div
          class="sticky bottom-0 z-10 mt-auto flex p-[.8rem] border-t border-line bg-fill-1"
          :class="{
            'flex-col': siderCollapsed,
            'items-center': siderCollapsed,
            'gap-[.6rem]': siderCollapsed,
            'flex-row': !siderCollapsed,
            'justify-between': !siderCollapsed && menuSettingTo,
            'justify-end': !siderCollapsed && !menuSettingTo,
          }"
        >
          <n-button
            v-if="menuSettingTo"
            quaternary
            circle
            :focusable="false"
            aria-label="菜单显示设置"
            @click="openMenuSetting"
          >
            <template #icon>
              <n-icon :size="18">
                <SettingsOutline />
              </n-icon>
            </template>
          </n-button>
          <n-button
            quaternary
            circle
            :focusable="false"
            :aria-label="siderCollapsed ? '展开菜单' : '收起菜单'"
            @click="toggleCollapsed"
          >
            <template #icon>
              <n-icon :size="18">
                <ReorderThreeOutline />
              </n-icon>
            </template>
          </n-button>
        </div>
      </div>
    </n-layout-sider>

    <n-layout content-class="flex flex-col">
      <n-layout-header
        class="h-[6.4rem] shrink-0 bg-fill-1 border-b border-line-strong flex items-center gap-[1.2rem] px-[1.2rem] md:gap-[3rem] md:px-[1.6rem]"
      >
        <!-- 窄屏汉堡按钮:打开侧栏抽屉 -->
        <n-button
          v-if="isMobile"
          quaternary
          circle
          :focusable="false"
          class="mr-auto"
          aria-label="打开菜单"
          @click="drawerOpen = true"
        >
          <template #icon>
            <n-icon :size="20">
              <MenuOutline />
            </n-icon>
          </template>
        </n-button>
        <div class="ml-auto flex items-center gap-[1.2rem] md:gap-[3rem]">
          <n-button
            quaternary
            circle
            class="lm-theme-toggle"
            :focusable="false"
            aria-label="切换主题"
            @click="handleToggleTheme"
          >
            <template #icon>
              <transition name="lm-theme-icon" mode="out-in">
                <span :key="darkMode ? 'dark' : 'light'" class="lm-theme-toggle-icon">
                  <n-icon :size="18">
                    <component :is="darkMode ? MoonOutline : SunnyOutline" />
                  </n-icon>
                </span>
              </transition>
            </template>
          </n-button>
          <slot name="header-extra" />
        </div>
      </n-layout-header>
      <n-layout-content class="bg-page" content-class="p-[1.6rem] flex flex-col">
        <!-- 默认插槽兜底为路由出口并带过渡;外部可传入自定义内容覆盖 -->
        <slot>
          <RouterView v-slot="{ Component }">
            <transition
              :name="pageTransition"
              mode="out-in"
              @before-leave="handleBeforeLeave"
              @after-leave="handleAfterLeave"
            >
              <component :is="Component" :key="route.path" />
            </transition>
          </RouterView>
        </slot>
        <!-- 页脚在内容流内:短内容时 mt-auto 贴底,长内容时跟随内容末尾滚动,不遮挡内容 -->
        <div v-if="footer" class="mt-auto pt-[1.6rem] text-center text-[1.2rem] text-tertiary">
          <LmFooterText :text="footer" />
        </div>
      </n-layout-content>
    </n-layout>
  </n-layout>
</template>

<style scoped>
/* === 窄屏抽屉模式(≤767px) ===
 * naive collapse-mode="width" 通过 width 过渡收起侧栏,但侧栏仍占文档流,
 * 窄屏下会把内容区挤到极窄。改由组件自身 transform 悬浮:
 * 侧栏 fixed 脱离布局流,平移收起/展开,内容区占满全宽。
 * 选择器直接命中子组件根元素(根节点继承父 scope id,无需 :deep);
 * naive 内联 width/collapsed-width 用 !important 覆盖。
 * 宽度与桌面侧栏一致(212px=21.2rem):保证菜单"图标+缩进+标题"完整展示不被省略 */
.lm-layout-sider--drawer {
  position: fixed !important;
  inset-block: 0;
  left: 0;
  z-index: 2002;
  width: 21.2rem !important;
  max-width: 21.2rem !important;
  transition:
    transform 0.25s ease,
    box-shadow 0.25s ease;
  box-shadow: 0 0 2.4rem rgba(0, 0, 0, 0.18);
}

/* 抽屉打开时的遮罩 */
.lm-layout-mask {
  position: fixed;
  inset: 0;
  z-index: 2001;
  background-color: rgba(0, 0, 0, 0.4);
  animation: lm-mask-in 0.2s ease;
}

@keyframes lm-mask-in {
  from {
    opacity: 0;
  }
}

/* === 侧栏菜单:圆角卡片式导航 ===
 * 对齐设计稿:分组展开态为圆角背景块,子级按层缩进并以树线串联,选中项用主色竖条 + 主色文字标识。
 * naive 的背景层是 .n-menu-item-content::before(自带左右 8px 内缩),圆角与配色由其 CSS 变量驱动;
 * ::after 未被 naive 占用,用于选中竖条。 */
.lm-sider-menu {
  /* naive 把菜单主题变量内联写在 .n-menu 的 style 上,类选择器须 !important 才能覆盖 */
  --n-border-radius: .8rem !important;
  /* 项高统一由主题 overrides.ts 的 Menu.itemHeight(3.6rem)驱动,此处不再硬编码,
     否则 !important 会压过主题内联值,出现"改了主题不生效"的假象 */
  --n-item-color-hover: var(--c-fill-4) !important;
  --n-item-color-active: var(--c-fill-4) !important;
  /* 折叠态仅剩图标:选中项改用主色软底块(实心主色会与彩色品牌标识互抢) */
  --n-item-color-active-collapsed: var(--c-primary-tint) !important;
}

/* 项间距收紧;分组之间比组内多留 1px 以区分层级(项高 36px 见主题 Menu.itemHeight) */
.lm-sider-menu :deep(.n-menu-item) {
  margin-top: .1rem;
}
.lm-sider-menu :deep(.n-submenu + .n-submenu) {
  margin-top: .2rem;
}
/* 首个分组不带上边距(naive 默认 6px):与上方搜索框/标识区的间距交给容器 padding 控制 */
.lm-sider-menu > :deep(.n-submenu:first-child) {
  margin-top: 0;
}

/* 背景块左右内缩 1rem(naive 默认 8px),与设计稿的分组圆角块对齐 */
.lm-sider-menu :deep(.n-menu-item-content)::before {
  left: 1rem;
  right: 1rem;
}

/* 子级缩进:图标与上一级的文字列对齐(一级 41px = 顶层文字起点,二级 65px = 一级文字起点),
   文字随之再进 2.4rem(图标 16 + 间距 8),层级递进与设计稿的"图标对上级文字"规律一致 */
.lm-sider-menu :deep(.n-submenu-children > .n-menu-item > .n-menu-item-content) {
  padding-left: 4.1rem !important;
}
.lm-sider-menu :deep(.n-submenu-children .n-submenu-children > .n-menu-item > .n-menu-item-content) {
  padding-left: 6.5rem !important;
}

/* 箭头右侧留白收窄(naive 默认 18px → 14px),使箭头中心距侧栏右边 22px,与设计稿一致 */
.lm-sider-menu :deep(.n-menu-item-content) {
  padding-right: 1.4rem;
}

/* 树线:子级容器左侧一条竖线串起同级项,位置取父级图标的图形中线(顶层 25px / 一级 49px) */
.lm-sider-menu :deep(.n-submenu-children) {
  position: relative;
}
.lm-sider-menu :deep(.n-submenu-children)::before {
  content: '';
  position: absolute;
  left: 2.5rem;
  top: .2rem;
  bottom: .2rem;
  width: 1px;
  background-color: var(--c-line);
  pointer-events: none;
}
.lm-sider-menu :deep(.n-submenu-children .n-submenu-children)::before {
  left: 4.9rem;
}

/* 展开的分组(含当前页所在分组)铺圆角背景块,标识"当前所处模块";折叠态窄栏里灰底不显眼,改用主色软底块 */
.lm-sider-menu:not(.n-menu--collapsed) :deep(.n-menu-item-content--child-active)::before {
  background-color: var(--c-fill-4);
}
.lm-sider-menu.n-menu--collapsed :deep(.n-menu-item-content--child-active)::before {
  background-color: var(--c-primary-tint);
}

/* 选中项:主色竖条标识当前页(不整块铺底,避免与展开分组的背景块混淆);悬浮时仍保留背景反馈 */
.lm-sider-menu :deep(.n-submenu-children .n-menu-item-content--selected:not(:hover))::before {
  background-color: transparent;
}
.lm-sider-menu :deep(.n-submenu-children .n-menu-item-content--selected)::after {
  content: '';
  position: absolute;
  left: 2.5rem;
  top: 50%;
  width: .3rem;
  height: 1.7rem;
  transform: translateY(-50%);
  border-radius: .2rem;
  background-color: var(--c-primary);
  pointer-events: none;
}
.lm-sider-menu :deep(.n-submenu-children .n-submenu-children .n-menu-item-content--selected)::after {
  left: 4.9rem;
}
.lm-sider-menu :deep(.n-menu-item-content--selected .n-menu-item-content-header) {
  color: var(--c-primary-text);
}
.lm-sider-menu :deep(.n-menu-item-content--selected .n-menu-item-content__icon) {
  color: var(--c-primary);
}

/* 折叠态选中/所在分组:图标跟随主色软底块 */
.lm-sider-menu.n-menu--collapsed :deep(.n-menu-item-content--selected .n-menu-item-content__icon),
.lm-sider-menu.n-menu--collapsed :deep(.n-menu-item-content--child-active .n-menu-item-content__icon) {
  color: var(--c-primary);
}

/* 隐藏侧边栏滚动条轨道:滚动发生在 n-scrollbar-container 上,隐藏轨道不影响滚轮滚动 */
.n-layout-sider :deep(.n-scrollbar-rail) {
  display: none;
}

/* 主题图标切换:旋转 + 缩放 + 淡入淡出 */
.lm-theme-icon-enter-active,
.lm-theme-icon-leave-active {
  transition:
    opacity 0.25s ease,
    transform 0.25s ease;
}
.lm-theme-icon-enter-from {
  opacity: 0;
  transform: rotate(-90deg) scale(0.5);
}
.lm-theme-icon-leave-to {
  opacity: 0;
  transform: rotate(90deg) scale(0.5);
}

/* 主题切换按钮:悬浮时图标轻微旋转放大,动画作用于内层 n-icon,避免与切换过渡的 transform 冲突 */
.lm-theme-toggle-icon {
  display: inline-flex;
}
.lm-theme-toggle :deep(.n-icon) {
  transition: transform 0.3s ease;
}
.lm-theme-toggle:hover :deep(.n-icon) {
  transform: rotate(20deg) scale(1.15);
}
</style>

<style>
/* 路由切换过渡:配合内置 RouterView 的 transition name="lm-page" */
.lm-page-enter-active,
.lm-page-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.lm-page-enter-from {
  opacity: 0;
  transform: translateX(8px);
}
.lm-page-leave-to {
  opacity: 0;
  transform: translateX(-8px);
}

/* 指定路由离场:页面以目标菜单项为原点被吸入,原点由 JS 以 CSS 变量注入;无目标时原地吸入 */
.lm-page-fly-enter-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.lm-page-fly-enter-from {
  opacity: 0;
  transform: translateX(8px);
}
.lm-page-fly-leave-active {
  animation: lm-page-suck 0.45s cubic-bezier(0.7, 0, 0.9, 0.5) forwards;
  transform-origin: var(--lm-fly-origin, center);
  will-change: transform, opacity;
}
/* 吸入:整体加速坍缩进目标点,透明度保持到末段才收掉 */
@keyframes lm-page-suck {
  0% {
    opacity: 1;
    transform: scale(1);
  }
  70% {
    opacity: 1;
  }
  100% {
    opacity: 0;
    transform: scale(0);
  }
}

/* 飞入完成后菜单项闪烁提示:引导用户从该菜单重新进入 */
.lm-menu-item-flash .n-menu-item-content {
  animation: lm-menu-item-flash 1.6s ease;
}
@keyframes lm-menu-item-flash {
  0%,
  100% {
    background-color: transparent;
  }
  25%,
  65% {
    background-color: rgba(var(--n-primary-color), 0.18);
  }
}

/* 主题切换:禁用 View Transitions 默认交叉淡入,由圆形 clip-path 动画接管 */
::view-transition-old(root),
::view-transition-new(root) {
  animation: none;
  mix-blend-mode: normal;
}
</style>
