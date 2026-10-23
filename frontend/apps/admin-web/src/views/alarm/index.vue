<script setup lang="ts">
import { useController } from './useController'

const { LmTableGroup, LmTable, LmPage, LmRefresh, LmColumn, openCreate } = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button type="primary" @click="openCreate"> 新建告警规则 </n-button>
        </template>
        <template #header-right>
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                description="还没有告警规则,新建一个开始使用"
                action-text="新建告警规则"
                @action="openCreate"
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
