<script setup lang="ts">
import GeneralSettings from './components/GeneralSettings.vue'
import MenuVisibility from './components/MenuVisibility.vue'
import BrandSettings from '@/views/brand/index.vue'

const route = useRoute()
const router = useRouter()

/** 合法的页签 name,用于校验 URL 中的 ?tab= */
const VALID_TABS = ['general', 'menu', 'brand'] as const

/**
 * 当前页签:由 URL ?tab= 驱动,缺失或非法时回退默认页签。
 * 这样收藏/外链/全局搜索等外部跳转经 brand 重定向带过来的
 * tab 参数可直接定位到对应页签;切换页签时同步回写 URL(默认页签不写,保持地址栏干净)。
 */
const activeTab = computed<string>({
  get: () => {
    const tab = route.query.tab
    return typeof tab === 'string' && (VALID_TABS as readonly string[]).includes(tab)
      ? tab
      : 'general'
  },
  set: (val) => {
    const query = { ...route.query }
    if (val === 'general') delete query.tab
    else query.tab = val
    void router.replace({ query })
  },
})
</script>

<template>
  <n-tabs v-model:value="activeTab" size="large" type="line" animated>
    <n-tab-pane name="general" tab="通用设置">
      <GeneralSettings />
    </n-tab-pane>
    <n-tab-pane name="brand" tab="品牌配置">
      <BrandSettings />
    </n-tab-pane>
    <n-tab-pane name="menu" tab="模块管理">
      <MenuVisibility />
    </n-tab-pane>
  </n-tabs>
</template>
