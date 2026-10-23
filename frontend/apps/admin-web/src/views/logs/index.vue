<script setup lang="ts">
import { useController } from './useController'
import AuditDetailDrawer from './AuditDetailDrawer.vue'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  LmSearch,
  param,
  actionFilterOptions,
  tenantFilterOptions,
  disableFutureDate,
  onActionChange,
  onTenantChange,
  onDateChange,
  hasActiveQuery,
  reset,
  activeLog,
  detailDrawerShow,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard>
      <LmTableGroup>
        <template #header-right>
          <LmSelect
            :value="param.action"
            :options="actionFilterOptions"
            class="!w-[10rem]"
            @update:value="onActionChange"
          />
          <LmSelect
            :value="param.tenant_id"
            :options="tenantFilterOptions"
            filterable
            clearable
            placeholder="选择租户"
            class="!w-[16rem]"
            @update:value="onTenantChange"
          />
          <n-date-picker
            :value="param.dateRange"
            type="daterange"
            clearable
            :is-date-disabled="disableFutureDate"
            class="!w-[26rem]"
            @update:value="onDateChange"
          />
          <LmSearch field="resource_id" placeholder="按资源 ID 精确搜索" class="!w-[18rem]" />
          <LmRefresh />
          <LmColumn />
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

    <!-- 审计事件详情抽屉 -->
    <AuditDetailDrawer v-model:show="detailDrawerShow" :log="activeLog" />
  </div>
</template>
