<script setup lang="ts">
import { useController } from './useController'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  LmSearch,
  param,
  statusFilterOptions,
  onStatusChange,
  openCreate,
  hasActiveQuery,
  reset,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button type="primary" @click="openCreate"> 新建API Key </n-button>
        </template>
        <template #header-right>
          <LmSelect
            :value="param.status"
            :options="statusFilterOptions"
            class="!w-[11rem]"
            @update:value="onStatusChange"
          />
          <LmSearch placeholder="搜索名称、Key 明文" class="!w-[18rem]" />
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="还没有 API Key,新建一个开始使用"
                action-text="新建API Key"
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
  </div>
</template>
