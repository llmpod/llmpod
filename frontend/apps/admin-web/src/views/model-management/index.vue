<script setup lang="ts">
import { useController } from './useController'

const { LmTableGroup, LmTable, LmPage, LmRefresh, LmColumn, openAdd } =
  useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <FlowGuideAlert />
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button
            type="primary"
            data-flow-guide="upload-model"
            @click="openAdd"
          >
            添加模型
          </n-button>
        </template>
        <template #header-right>
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                description="还没有模型,添加一个开始使用"
                action-text="添加模型"
                @action="openAdd"
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
