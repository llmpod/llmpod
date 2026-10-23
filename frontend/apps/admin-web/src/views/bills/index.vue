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
  tenantOptions,
  disableFutureDate,
  param,
  onStatusChange,
  onTenantIdChange,
  onDateRangeChange,
  hasActiveQuery,
  reset,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard>
      <LmTableGroup>
        <template #header-right>
          <LmSelect
            :value="param.status"
            :options="statusFilterOptions"
            class="!w-[11rem]"
            @update:value="onStatusChange"
          />
          <LmSelect
            :value="param.tenant_id"
            :options="tenantOptions"
            filterable
            clearable
            placeholder="选择租户"
            class="!w-[16rem]"
            @update:value="onTenantIdChange"
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
            placeholder="搜索模型别名"
            class="!w-[16rem]"
          />
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="暂无调用日志"
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
