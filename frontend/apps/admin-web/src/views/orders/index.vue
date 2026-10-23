<script setup lang="ts">
import { useController } from './useController'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  LmSearch,
  statCards,
  statusFilterOptions,
  param,
  onStatusChange,
  hasActiveQuery,
  reset,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 统计卡片:窄屏 2 列降级,避免数值挤压换行 -->
    <div class="grid grid-cols-2 gap-[.75rem] md:grid-cols-4">
      <LmCard v-for="card in statCards" :key="card.label">
        <div class="stat-subtitle">
          {{ card.label }}
        </div>
        <div class="stat-value" :class="card.tone ? `stat-text--${card.tone}` : ''">
          {{ card.value }}
        </div>
      </LmCard>
    </div>

    <!-- 订单列表 -->
    <LmCard>
      <LmTableGroup>
        <template #header-right>
          <LmSelect
            :value="param.status"
            :options="statusFilterOptions"
            class="!w-[11rem]"
            @update:value="onStatusChange"
          />
          <LmSearch field="keyword" placeholder="搜索订单号、租户" class="!w-[16rem]" />
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="暂无订单"
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

<style scoped>
.stat-subtitle {
  font-size: 14px;
  color: var(--c-default);
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-strong);
}

.stat-text--warning {
  color: rgb(var(--n-warning-color));
}

.stat-text--error {
  color: rgb(var(--n-error-color));
}
</style>
