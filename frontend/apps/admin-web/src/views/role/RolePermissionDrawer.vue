<script setup lang="ts">
import { message, useConfirm } from '@repo/hooks'
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

const { loadPermissions, getRolePermissions, saveRolePermissions, toPermissionId } = useStore()

const loading = ref(false)
const saving = ref(false)
/** 权限加载失败标记:失败时勾选集停留空集,禁用保存防空权限全量替换清空该角色 */
const loadError = ref(false)
/** 树勾选节点(分组节点 key 为字符串,权限叶子 key 为权限ID) */
const checkedKeys = ref<Array<string | number>>([])
/** 已授权但无法映射到权限清单的串(清单外权限点);非空时保存需二次确认,防全量替换静默撤销 */
const unmappedKeys = ref<string[]>([])
/** 加载序号:快速切换角色时丢弃迟到的旧响应,防 A 角色的权限回显覆盖 B 角色 */
let loadSeq = 0

/**
 * 加载权限清单与角色已授权权限并回显勾选。
 * 失败置 loadError 并禁用保存:catch 前勾选集已被清空,若不拦截,
 * 一次保存就会把空集当目标全集写回,静默清空该角色全部权限。
 */
async function loadRolePermission(): Promise<void> {
  if (!props.role) return
  const seq = ++loadSeq
  loading.value = true
  loadError.value = false
  checkedKeys.value = []
  unmappedKeys.value = []
  try {
    // 每次打开强制重载权限清单:防模块级旧缓存漏掉后端新增权限点,导致保存时误撤销
    const [, granted] = await Promise.all([
      loadPermissions(true),
      getRolePermissions(props.role.id),
    ])
    if (seq !== loadSeq) return // 已切换到其它角色,丢弃本次迟到响应
    const ids: number[] = []
    const unmapped: string[] = []
    for (const key of granted) {
      const id = toPermissionId(key)
      if (id !== undefined) ids.push(id)
      else unmapped.push(key)
    }
    checkedKeys.value = ids
    unmappedKeys.value = unmapped
  } catch {
    // 错误提示由 http 层全局 onError 弹出,此处仅标记状态拦截保存
    if (seq === loadSeq) loadError.value = true
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** 打开抽屉时加载权限清单与角色已授权权限,并回显勾选 */
watch(
  () => props.show,
  (show) => {
    if (show && props.role) void loadRolePermission()
  },
  { immediate: true },
)

/** 保存:勾选的权限叶子合集即为目标全集(全量替换) */
async function onSave() {
  if (!props.role) return
  if (loading.value) return
  // 加载失败勾选集不可信,禁止以空集保存
  if (loadError.value) return
  // 存在清单外已授权串时显式确认:replace 只收 ID,未识别串无法保留,保存即移除
  if (unmappedKeys.value.length > 0) {
    const preview = unmappedKeys.value.slice(0, 3).join('、')
    const ok = await useConfirm({
      title: '存在无法识别的已授权权限',
      content: `${unmappedKeys.value.length} 条权限不在当前权限清单内(${preview}${unmappedKeys.value.length > 3 ? ' 等' : ''}),保存后将从该角色移除。是否继续?`,
    }).then(
      () => true,
      () => false,
    )
    if (!ok) return
  }
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
  <n-drawer v-model:show="innerShow" :width="640">
    <n-drawer-content :title="`角色权限——${role?.name ?? ''}`" closable>
      <n-spin :show="loading">
        <div v-if="role" class="flex flex-col gap-[1rem]">
          <!-- 角色信息 -->
          <section>
            <div class="drawer-subtitle">角色信息</div>
            <p class="drawer-text">
              编码:{{ role.code ?? '—' }} ·
              {{ role.is_builtin ? '内置角色' : '自定义角色' }}
            </p>
          </section>

          <!-- 清单外权限警告:这些已授权串无法在树上勾选,保存将被移除 -->
          <n-alert v-if="unmappedKeys.length > 0" type="warning" :show-icon="true">
            {{ unmappedKeys.length }} 条已授权权限不在当前清单内,保存后将从该角色移除
          </n-alert>

          <!-- 权限树 -->
          <section>
            <div class="drawer-subtitle">权限配置</div>
            <div v-if="loadError" class="flex flex-col items-start gap-[.75rem] p-[.5rem]">
              <n-alert type="error" :show-icon="true">
                权限加载失败,保存已禁用,以防空勾选覆盖当前配置
              </n-alert>
              <n-button size="small" @click="loadRolePermission"> 重新加载 </n-button>
            </div>
            <div v-else class="p-[.5rem]">
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
          <n-button @click="innerShow = false"> 取消 </n-button>
          <n-button
            type="primary"
            :loading="saving"
            :disabled="loading || loadError"
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
