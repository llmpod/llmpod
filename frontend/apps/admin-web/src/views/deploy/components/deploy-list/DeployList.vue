<script setup lang="ts">
import CreateWizard from './create-wizard/index.vue'
import DeployDetailView from './deploy-detail/index.vue'
import { useController } from './useController'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmBatch,
  statusOptions,
  param,
  detailViewRecord,
  detailEntryPending,
  closeDetailView,
  hasActiveQuery,
  showFirstDeployAlert,
  reset,
  wizardOpen,
  wizardInitialModelId,
  openCreate,
  onWizardExit,
  openWizard,
  handleStatusChange,
  handleBatch,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 子视图切换:部署详情(拓扑视图)/新建向导 覆盖 提示条+表格 区域,退出后回到表格 -->
    <Transition :name="wizardOpen || detailViewRecord ? 'view-slide-right' : 'view-slide-left'" mode="out-in">
      <LmCard v-if="detailViewRecord" key="detail">
        <DeployDetailView
          :key="detailViewRecord.deployment_id"
          :record="detailViewRecord"
          @exit="closeDetailView"
        />
      </LmCard>
      <LmCard v-else-if="wizardOpen" key="wizard">
        <!-- 确认部署成功时 exit 携带创建记录,向导关闭后直接切到该部署的详情视图;initial-model-id 入口预选模型 -->
        <CreateWizard :initial-model-id="wizardInitialModelId" @exit="onWizardExit" />
      </LmCard>
      <!-- 入口详情在途(?action=detail):按拓扑详情视图结构占位(头部操作行 + 端点/步骤条 + 画布),详情返回后切换不突兀 -->
      <LmCard
        v-else-if="detailEntryPending"
        key="entry-pending"
      >
        <!-- 头部:返回按钮 + 标题 + 状态标签 + 详情按钮(对齐 DeployDetailView) -->
        <div class="flex items-center gap-[.8rem]">
          <n-skeleton
            :width="'6.4rem'"
            :height="'2.8rem'"
            :sharp="false"
          />
          <n-skeleton
            text
            :width="'16rem'"
            style="font-size: 1.6rem"
          />
          <n-skeleton
            :width="'4.8rem'"
            :height="'2.2rem'"
            :sharp="false"
          />
          <n-skeleton
            :width="'6.4rem'"
            :height="'2.8rem'"
            :sharp="false"
          />
        </div>
        <!-- 端点条/步骤条区域 -->
        <n-skeleton
          class="mt-[1.2rem]"
          :height="'3.6rem'"
          :sharp="false"
        />
        <!-- 拓扑画布区:高度与真实画布一致(calc + min-h) -->
        <div class="mt-[1.6rem] h-[calc(100vh-33rem)] min-h-[42rem] rounded-lg border border-line overflow-hidden">
          <n-skeleton
            height="100%"
            :sharp="false"
          />
        </div>
      </LmCard>
      <div v-else key="list" class="flex flex-col gap-[.75rem]">
        <FlowGuideAlert />
        <LmAlert v-if="showFirstDeployAlert" type="warning">
          首次部署建议使用部署向导,它会引导您跑动整个流程
        </LmAlert>
        <LmCard>
          <LmTableGroup>
            <template #header-left>
              <n-button type="primary" data-flow-guide="create-deploy" @click="openCreate()">
                新建部署
              </n-button>
              <n-button @click="openWizard"> 部署向导 </n-button>
            </template>
            <template #header-right>
              <LmSelect
                :value="param.status"
                :options="statusOptions"
                class="!w-[16rem]"
                @update:value="handleStatusChange"
              />
            </template>
            <template #content>
              <LmTable>
                <template #empty>
                  <LmTableEmpty
                    v-if="!hasActiveQuery"
                    description="还没有部署,新建一个开始使用"
                    action-text="新建部署"
                    @action="openCreate()"
                  />
                  <LmTableEmpty
                    v-else
                    description="未找到匹配的结果"
                    action-text="清空筛选"
                    @action="reset"
                  />
                </template>
              </LmTable>
            </template>
            <template #footer-left>
              <LmBatch @select="handleBatch" />
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
/* 表格视图/新建向导切换过渡:进入向导向右滑、退出向左滑,节奏与 node-pool view-slide 一致 */
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
