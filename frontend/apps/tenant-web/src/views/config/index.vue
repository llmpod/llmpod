<script setup lang="ts">
import ProfileSettings from './components/ProfileSettings.vue'
import MenuVisibility from './components/MenuVisibility.vue'

const route = useRoute()
const router = useRouter()

/** 合法的页签 name,用于校验 URL 中的 ?tab= */
const VALID_TABS = ['profile', 'menu'] as const

/**
 * 当前页签:由 URL ?tab= 驱动,缺失或非法时回退默认页签。
 * 全局搜索等外部跳转带过来的 tab 参数可直接定位到对应页签;
 * 切换页签时同步回写 URL(默认页签不写,保持地址栏干净)。
 */
const activeTab = computed<string>({
  get: () => {
    const tab = route.query.tab
    return typeof tab === 'string' && (VALID_TABS as readonly string[]).includes(tab)
      ? tab
      : 'profile'
  },
  set: (val) => {
    const query = { ...route.query }
    if (val === 'profile') delete query.tab
    else query.tab = val
    void router.replace({ query })
  },
})
</script>

<template>
  <n-tabs v-model:value="activeTab" size="large" type="line" animated>
    <n-tab-pane name="profile" tab="个人设置">
      <ProfileSettings />
    </n-tab-pane>
    <n-tab-pane
      name="menu"
      tab="模块管理"
    >
      <MenuVisibility />
    </n-tab-pane>
  </n-tabs>
</template>
