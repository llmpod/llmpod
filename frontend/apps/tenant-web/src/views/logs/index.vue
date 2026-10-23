<script setup lang="ts">
import { useController } from './useController'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmSearch,
  LmRefresh,
  AuditDetailDrawer,
  param,
  actionFilterOptions,
  disableFutureDate,
  onActionChange,
  onDateChange,
  hasActiveQuery,
  resetFilters,
  detailShow,
  activeLog,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 租户范围提示 -->
    <n-card>
      <span class="text-sm font-semibold text-strong mr-[1rem]">审计日志</span>
      <span class="text-sm text-secondary">仅显示本租户操作记录</span>
    </n-card>

    <!-- 审计日志列表 -->
    <n-card>
      <LmTableGroup>
        <template #header-right>
          <LmSelect
            :value="param.action"
            :options="actionFilterOptions"
            class="!w-[10rem]"
            @update:value="onActionChange"
          />
          <n-date-picker
            :value="param.dateRange"
            type="daterange"
            clearable
            :is-date-disabled="disableFutureDate"
            class="!w-[26rem]"
            @update:value="onDateChange"
          />
          <LmSearch
            field="resource_id"
            placeholder="按资源 ID 精确搜索"
            class="!w-[18rem]"
          />
          <LmRefresh />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="暂无日志"
              />
              <LmTableEmpty
                v-else
                description="未找到匹配的结果"
                action-text="清空筛选"
                @action="resetFilters"
              />
            </template>
          </LmTable>
        </template>
        <template #footer-right>
          <LmPage />
        </template>
      </LmTableGroup>
    </n-card>

    <!-- 审计事件详情抽屉 -->
    <AuditDetailDrawer
      v-model:show="detailShow"
      :log="activeLog"
    />
  </div>
</template>
