<script setup lang="ts">
import { useController } from './useController'
import TopologyView from './topology/TopologyView.vue'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  topologyPool,
  entryPending,
  openCreate,
  closeTopology,
} = useController()
</script>

<template>
  <div>
    <!-- 拓扑视图:点击行操作「详情」后整页切换,返回按钮/退出事件回到表格;进入向右滑入、退出向左滑回 -->
    <Transition :name="topologyPool ? 'view-slide-right' : 'view-slide-left'" mode="out-in">
      <TopologyView
        v-if="topologyPool"
        :key="topologyPool.id"
        :cluster-id="topologyPool.id"
        :cluster-name="topologyPool.name"
        @exit="closeTopology"
      />
      <!-- 拓扑直达在途(?topology=):按拓扑视图结构占位(头部操作行 + 画布),数据到达后直接展开拓扑,不闪列表 -->
      <LmCard
        v-else-if="entryPending"
        key="entry-pending"
      >
        <!-- 头部:返回按钮 + 集群下拉 + 模式切换(对齐 TopologyView) -->
        <div class="flex items-center gap-[1.6rem] mb-[1.2rem]">
          <n-skeleton
            :width="'5.6rem'"
            :height="'2.8rem'"
            :sharp="false"
          />
          <n-skeleton
            :width="'18rem'"
            :height="'2.8rem'"
            :sharp="false"
          />
          <n-skeleton
            :width="'10rem'"
            :height="'2.8rem'"
            :sharp="false"
          />
        </div>
        <!-- 拓扑画布区:高度与真实画布一致(calc + min-h) -->
        <div class="h-[calc(100vh-20rem)] min-h-[48rem] rounded-lg border border-line overflow-hidden">
          <n-skeleton
            height="100%"
            :sharp="false"
          />
        </div>
      </LmCard>
      <div v-else class="flex flex-col gap-[.75rem]">
        <FlowGuideAlert />
        <LmCard>
          <LmTableGroup>
            <template #header-left>
              <n-button type="primary" data-flow-guide="create-node-pool" @click="openCreate"> 创建集群 </n-button>
            </template>
            <template #header-right>
              <LmRefresh />
              <LmColumn />
            </template>
            <template #content>
              <LmTable>
                <template #empty>
                  <LmTableEmpty
                    description="还没有集群,创建一个开始使用"
                    action-text="创建集群"
                    @action="openCreate"
                  />
                </template>
              </LmTable>
            </template>
            <template #footer-right>
              <LmPage />
            </template>
          </LmTableGroup>
        </LmCard>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
/* 顶部提示图标:尺寸跟随 n-alert 图标字号,配色由 currentColor 继承 */
.pool-alert-icon {
  width: 2.4rem;
  height: 2.4rem;
  display: block;
}

/* 表格视图/拓扑视图整页切换过渡:进入拓扑向右滑、退出向左滑,节奏与拓扑详情面板 panel-slide 一致 */
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
