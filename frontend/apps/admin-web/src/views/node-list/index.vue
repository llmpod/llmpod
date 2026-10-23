<script setup lang="ts">
import { useController } from './useController'
import NodeGpuDrawer from './NodeGpuDrawer.vue'
import NodeDetailDrawer from './NodeDetailDrawer.vue'
import NodeCardList from './NodeCardList.vue'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  hasActiveQuery,
  reset,
  openRegister,
  activeNode,
  gpuDrawerShow,
  detailDrawerShow,
  viewMode,
  nodes,
  loading,
  openGpuDrawer,
  openDetailDrawer,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <FlowGuideAlert />
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button type="primary" data-flow-guide="register-node" @click="openRegister">
            注册节点
          </n-button>
        </template>
        <template #header-right>
          <!-- 临时隐藏搜索:后端不支持 keyword,仅过滤当前页导致跨页搜索失效(F-002),待后端支持后恢复 -->
          <!-- <LmSearch field="keyword" placeholder="搜索节点" class="w-[16rem]" /> -->
          <n-radio-group v-model:value="viewMode" size="small">
            <n-radio-button value="table">列表模式</n-radio-button>
            <n-radio-button value="card">卡片模式</n-radio-button>
          </n-radio-group>
          <LmRefresh />
          <LmColumn v-if="viewMode === 'table'" />
        </template>
        <template #content>
          <!-- 列表/卡片模式切换过渡:切到卡片向右滑入、切回列表向左滑回,节奏与 node-pool view-slide 一致 -->
          <Transition :name="viewMode === 'card' ? 'view-slide-right' : 'view-slide-left'" mode="out-in">
            <div v-if="viewMode === 'table'" key="table">
              <LmTable>
                <template #empty>
                  <LmTableEmpty
                    v-if="!hasActiveQuery"
                    description="还没有节点,注册一个开始使用"
                    action-text="注册节点"
                    @action="openRegister"
                  />
                  <LmTableEmpty
                    v-else
                    description="未找到匹配的结果"
                    action-text="清空筛选"
                    @action="reset"
                  />
                </template>
              </LmTable>
            </div>
            <div v-else key="card">
              <NodeCardList
                :nodes="nodes"
                :loading="loading"
                :has-active-query="hasActiveQuery"
                @gpu="openGpuDrawer"
                @detail="openDetailDrawer"
                @reset="reset"
                @register="openRegister"
              />
            </div>
          </Transition>
        </template>
        <template #footer-right>
          <LmPage />
        </template>
      </LmTableGroup>
    </LmCard>
    <!-- GPU 管理 / 节点详情抽屉 -->
    <NodeGpuDrawer v-model:show="gpuDrawerShow" :node="activeNode" />
    <NodeDetailDrawer v-model:show="detailDrawerShow" :node="activeNode" />
  </div>
</template>

<style scoped>
/* 顶部提示图标:尺寸跟随 n-alert 图标字号,配色由 currentColor 继承 */
.node-alert-icon {
  width: 2.4rem;
  height: 2.4rem;
  display: block;
}

/* 列表/卡片模式切换过渡:切到卡片向右滑、切回列表向左滑,节奏与 node-pool view-slide 一致 */
.view-slide-right-enter-active,
.view-slide-right-leave-active,
.view-slide-left-enter-active,
.view-slide-left-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.view-slide-right-enter-from {
  opacity: 0;
  transform: translateX(-1.6rem);
}
.view-slide-right-leave-to {
  opacity: 0;
  transform: translateX(1.6rem);
}
.view-slide-left-enter-from {
  opacity: 0;
  transform: translateX(1.6rem);
}
.view-slide-left-leave-to {
  opacity: 0;
  transform: translateX(-1.6rem);
}
</style>
