<script setup lang="ts">
import { useController } from './useController'

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
} = useController()
</script>

<template>
  <LmCard>
    <LmTableGroup>
      <template #header-left>
        <n-button type="primary" data-flow-guide="endpoint-create" @click="openCreate">
          接入端点
        </n-button>
      </template>
      <template #header-right>
        <LmSearch placeholder="搜索名称、模型、供应商" class="!w-[22rem]" />
        <LmRefresh />
        <LmColumn />
      </template>
      <template #content>
        <LmTable>
          <template #empty>
            <LmTableEmpty
              v-if="!hasActiveQuery"
              description="还没有接入端点，点击下方接入"
              action-text="接入端点"
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
</template>
