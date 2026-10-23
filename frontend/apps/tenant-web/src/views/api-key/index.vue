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
  hasActiveQuery,
  reset,
  openCreate,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 安全提示 -->
    <n-alert
      type="warning"
      :bordered="false"
    >
      请妥善保管您的 API
      Key，不要将其泄露给他人或在客户端代码中明文存储。建议定期轮换密钥，仅授予必要的模型访问权限。
    </n-alert>

    <n-card>
      <LmTableGroup>
        <template #header-left>
          <n-button
            type="primary"
            @click="openCreate"
          >
            创建API Key
          </n-button>
        </template>
        <template #header-right>
          <LmSelect
            :value="param.status"
            :options="statusFilterOptions"
            class="!w-[12rem]"
            @update:value="onStatusChange"
          />
          <LmSearch
            placeholder="搜索名称"
            class="!w-[28rem]"
          />
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                v-if="!hasActiveQuery"
                description="还没有 API Key,创建一个开始使用"
                action-text="创建API Key"
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
    </n-card>
  </div>
</template>
