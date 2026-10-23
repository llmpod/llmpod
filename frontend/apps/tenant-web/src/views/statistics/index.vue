<script setup lang="ts">
import { useController } from './useController'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  LmSearch,
  statusFilterOptions,
  apiKeyOptions,
  disableFutureDate,
  param,
  onStatusChange,
  onApiKeyChange,
  onDateRangeChange,
  hasActiveQuery,
  reset,
} = useController()
</script>

<template>
  <n-card>
    <LmTableGroup>
      <template #header-right>
        <LmSelect
          :value="param.status"
          :options="statusFilterOptions"
          class="!w-[12rem]"
          @update:value="onStatusChange"
        />
        <LmSelect
          :value="param.api_key_id"
          :options="apiKeyOptions"
          placeholder="全部 Key"
          clearable
          filterable
          class="!w-[16rem]"
          @update:value="onApiKeyChange"
        />
        <n-date-picker
          :value="param.dateRange"
          type="datetimerange"
          clearable
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          :default-time="['00:00:00', '23:59:59']"
          :is-date-disabled="disableFutureDate"
          class="!w-[26rem]"
          @update:value="onDateRangeChange"
        />
        <LmSearch
          field="model"
          placeholder="按模型别名筛选"
          class="!w-[20rem]"
        />
        <LmRefresh />
        <LmColumn />
      </template>
      <template #content>
        <LmTable>
          <template #empty>
            <LmTableEmpty
              v-if="!hasActiveQuery"
              description="暂无调用记录,使用 API Key 发起请求后这里会展示计量明细"
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
  </n-card>
</template>
