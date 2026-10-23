<script setup lang="ts">
import { useController } from './useController'
import RolePermissionDrawer from './RolePermissionDrawer.vue'

const {
  LmTableGroup,
  LmTable,
  LmPage,
  LmRefresh,
  LmColumn,
  openCreate,
  activeRole,
  permissionDrawerShow,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button
            type="primary"
            @click="openCreate"
          >
            新建自定义角色
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
                description="还没有自定义角色,新建一个开始使用"
                action-text="新建自定义角色"
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

    <!-- 角色权限抽屉 -->
    <RolePermissionDrawer
      v-model:show="permissionDrawerShow"
      :role="activeRole"
    />
  </div>
</template>
