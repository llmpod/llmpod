<script setup lang="ts">
import { useController } from '../useController'
import BindingDrawer from './BindingManage.vue'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  LmSearch,
  hasActiveQuery,
  reset,
  openCreate,
  activePublish,
  bindingDrawerShow,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <FlowGuideAlert />
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button type="primary" data-flow-guide="release-model" @click="openCreate">
            发布模型
          </n-button>
        </template>
        <template #header-right>
          <LmSearch field="keyword" placeholder="搜索对外模型名" class="!w-[16rem]" />
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="还没有已发布的模型"
                action-text="发布模型"
                @action="openCreate"
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
        <template #footer-right>
          <LmPage />
        </template>
      </LmTableGroup>
    </LmCard>
    <!-- 供应商抽屉 -->
    <BindingDrawer v-model:show="bindingDrawerShow" :publish="activePublish" />
  </div>
</template>
