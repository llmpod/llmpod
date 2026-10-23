<script setup lang="ts">
import { useController } from './useController'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmSearch,
  MemberDetailDrawer,
  status,
  statusFilterOptions,
  hasActiveQuery,
  resetFilters,
  statCards,
  detailShow,
  detailMember,
  openCreate,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 统计卡片 -->
    <div class="grid grid-cols-3 gap-[.75rem]">
      <LmCard
        v-for="card in statCards"
        :key="card.label"
      >
        <div class="stat-subtitle">
          {{ card.label }}
        </div>
        <div
          class="stat-value"
          :class="card.tone === 'error' ? 'stat-text--error' : ''"
        >
          {{ card.value }}
        </div>
      </LmCard>
    </div>

    <!-- 成员列表 -->
    <n-card>
      <LmTableGroup>
        <template #header-left>
          <n-button
            type="primary"
            @click="openCreate"
          >
            创建成员
          </n-button>
        </template>
        <template #header-right>
          <!-- TODO: 邀请成员接口未提供,按钮暂时隐藏,待接口就绪后恢复 -->
          <LmSelect
            v-model:value="status"
            :options="statusFilterOptions"
            class="!w-[12rem]"
          />
          <LmSearch
            placeholder="搜索用户名、邮箱"
            class="!w-[28rem]"
          />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="还没有成员,创建一个开始使用"
                action-text="创建成员"
                @action="openCreate"
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

    <!-- 成员详情抽屉 -->
    <MemberDetailDrawer
      v-model:show="detailShow"
      :member="detailMember"
    />
  </div>
</template>

<style scoped>
.stat-subtitle {
  font-size: 12px;
  color: var(--c-secondary);
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-strong);
}

.stat-text--error {
  color: rgb(var(--n-error-color));
}
</style>
