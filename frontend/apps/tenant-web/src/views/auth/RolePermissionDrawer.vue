<script setup lang="ts">
import { message } from '@repo/hooks'
import { permissionTreeData, useStore, type RoleData } from './useStore'

const props = defineProps<{
  show: boolean
  role: RoleData | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const { loadPermissions, saveRolePermissions } = useStore()

const loading = ref(false)
const saving = ref(false)
/** 树勾选节点(分组节点 key 为字符串,权限叶子 key 为权限ID) */
const checkedKeys = ref<Array<string | number>>([])

/** 打开抽屉时加载权限清单,并按角色已授权 permission_ids 回显勾选 */
watch(
  () => props.show,
  async (show) => {
    if (!show || !props.role) return
    loading.value = true
    checkedKeys.value = [...props.role.permission_ids]
    try {
      await loadPermissions()
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

/** 保存:勾选的权限叶子合集即为目标全集(全量替换) */
async function onSave() {
  if (!props.role) return
  saving.value = true
  try {
    // 分组节点 key 为字符串,只保留权限叶子ID
    const ids = checkedKeys.value.filter((k): k is number => typeof k === 'number')
    await saveRolePermissions(props.role.id, ids)
    message.success(`已保存:${props.role.name} 的权限配置`)
    innerShow.value = false
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <n-drawer
    v-model:show="innerShow"
    :width="640"
  >
    <n-drawer-content
      :title="`角色权限——${role?.name ?? ''}`"
      closable
    >
      <n-spin :show="loading">
        <div
          v-if="role"
          class="flex flex-col gap-[1rem]"
        >
          <!-- 角色信息 -->
          <section>
            <div class="drawer-subtitle">
              角色信息
            </div>
            <p class="drawer-text">
              编码:{{ role.code ?? '—' }} ·
              {{ role.is_builtin ? '内置角色' : '自定义角色' }}
            </p>
          </section>

          <!-- 权限树 -->
          <section>
            <div class="drawer-subtitle">
              权限配置
            </div>
            <div class="p-[.5rem]">
              <n-tree
                v-model:checked-keys="checkedKeys"
                :data="permissionTreeData"
                checkable
                cascade
                :selectable="false"
                block-line
                default-expand-all
              />
            </div>
          </section>
        </div>
      </n-spin>

      <template #footer>
        <div class="flex justify-end gap-[.5rem]">
          <n-button @click="innerShow = false">
            取消
          </n-button>
          <n-button
            type="primary"
            :loading="saving"
            :disabled="loading"
            @click="onSave"
          >
            保存
          </n-button>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.drawer-subtitle {
  font-size: 14px;
  font-weight: 600;
  color: var(--c-strong);
  margin-bottom: 8px;
}

.drawer-text {
  font-size: 13px;
  color: var(--c-secondary);
}
</style>
