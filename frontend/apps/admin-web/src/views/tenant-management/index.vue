<script setup lang="ts">
import { useController } from './useController'

const {
  overviewCards,
  overviewLoading,
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  LmSearch,
  param,
  tenantStatusFilterOptions,
  onStatusChange,
  goTenantLogin,
  hasActiveQuery,
  reset,
  openCreate,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 顶部概览统计卡(并入的原「租户概览」统计卡):窄屏 2 列降级 -->
    <div class="grid grid-cols-2 gap-[.75rem] md:grid-cols-4">
      <template v-if="overviewLoading">
        <LmCard v-for="i in 4" :key="i">
          <n-skeleton
            text
            style="display: block; width: 60px; font-size: 14px; margin-bottom: 12.4px"
          />
          <n-skeleton
            text
            style="display: block; width: 42px; font-size: 20px; margin-bottom: 12px"
          />
        </LmCard>
      </template>
      <template v-else>
        <LmCard v-for="card in overviewCards" :key="card.label">
          <div class="stat-subtitle">
            {{ card.label }}
          </div>
          <div class="stat-value" :class="card.tone ? `stat-text--${card.tone}` : ''">
            {{ card.value }}
          </div>
        </LmCard>
      </template>
    </div>

    <!-- 租户列表 -->
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button
            type="primary"
            @click="openCreate"
          >
            新增租户
          </n-button>
          <n-button @click="goTenantLogin">
            跳转租户登录
          </n-button>
        </template>
        <template #header-right>
          <LmSelect
            :value="param.status"
            :options="tenantStatusFilterOptions"
            class="!w-[11rem]"
            @update:value="onStatusChange"
          />
          <LmSearch
            field="keyword"
            placeholder="搜索租户名称"
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
                description="还没有租户,新增一个开始使用"
                action-text="新增租户"
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

.stat-text--success {
  color: rgb(var(--n-success-color));
}

.stat-text--error {
  color: rgb(var(--n-error-color));
}
</style>
