<script setup lang="ts">
import ModelRelease from './components/ModelRelease.vue'
import EndpointManage from './components/endpoint/EndpointManage.vue'

const route = useRoute()
const router = useRouter()

/** 合法的页签 name,用于校验 URL 中的 ?tab= */
const VALID_TABS = ['release', 'provider'] as const

/**
 * 当前页签:由 URL ?tab= 驱动,缺失或非法时回退默认页签。
 * 流程引导、全局搜索等外部跳转带过来的 tab 参数可直接定位到对应页签;
 * 切换页签时同步回写 URL(默认页签不写,保持地址栏干净)。
 * tab=endpoint 为「来源端点」旧名,改名供应商管理后重定向到 provider。
 */
const activeTab = computed<string>({
  get: () => {
    const tab = route.query.tab
    const normalized = tab === 'endpoint' ? 'provider' : tab
    return typeof normalized === 'string' && (VALID_TABS as readonly string[]).includes(normalized)
      ? normalized
      : 'release'
  },
  set: (val) => {
    const query = { ...route.query }
    if (val === 'release') delete query.tab
    else query.tab = val
    void router.replace({ query })
  },
})
</script>

<template>
  <n-tabs v-model:value="activeTab" size="large" type="line" animated>
    <n-tab-pane name="release" tab="模型发布">
      <ModelRelease />
    </n-tab-pane>
    <n-tab-pane name="provider" tab="供应商管理">
      <EndpointManage />
    </n-tab-pane>
  </n-tabs>
</template>