<script setup lang="ts">
import DeployList from './components/deploy-list/DeployList.vue'
import ModelManagement from '@/views/model-management/index.vue'

const route = useRoute()
const router = useRouter()

/** 合法的页签 name,用于校验 URL 中的 ?tab= */
const VALID_TABS = ['list', 'management'] as const

/**
 * 当前页签:由 URL ?tab= 驱动,缺失或非法时回退默认页签。
 * 这样流程引导、全局搜索等外部跳转经 model-management 重定向带过来的
 * tab 参数可直接定位到对应页签;切换页签时同步回写 URL(默认页签不写,保持地址栏干净)。
 */
const activeTab = computed<string>({
  get: () => {
    const tab = route.query.tab
    return typeof tab === 'string' && (VALID_TABS as readonly string[]).includes(tab)
      ? tab
      : 'list'
  },
  set: (val) => {
    const query = { ...route.query }
    if (val === 'list') delete query.tab
    else query.tab = val
    void router.replace({ query })
  },
})
</script>

<template>
  <n-tabs v-model:value="activeTab" size="large" type="line" animated>
    <n-tab-pane name="list" tab="部署列表">
      <DeployList />
    </n-tab-pane>
    <n-tab-pane name="management" tab="模型管理">
      <ModelManagement />
    </n-tab-pane>
  </n-tabs>
</template>

<style>
/* 页签切换过渡:与路由切换过渡(name="lm-page")保持一致,见 packages/naive-ui LmLayout */
.tab-page-enter-active,
.tab-page-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.tab-page-enter-from {
  opacity: 0;
  transform: translateX(0.8rem);
}
.tab-page-leave-to {
  opacity: 0;
  transform: translateX(-0.8rem);
}
</style>
