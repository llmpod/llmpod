<script setup lang="ts">
import { listRoles } from '@/api/access'
import {
  userStatusMap,
  useStore,
  formatDateTime,
  type UserData,
} from './useStore'

const props = defineProps<{
  show: boolean
  user: UserData | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const { getDetail } = useStore()
const detail = ref<UserData>()
const loading = ref(false)
/** 角色 ID → 名称映射(随抽屉打开与详情一并刷新) */
const roleNames = ref<Map<number, string>>(new Map())

watch(
  () => props.show,
  async (visible) => {
    if (!visible || !props.user) return
    loading.value = true
    try {
      const [userDetail, roles] = await Promise.all([getDetail(props.user.id), listRoles()])
      detail.value = userDetail
      roleNames.value = new Map(roles.map((role) => [role.id, role.name]))
    }
    finally {
      loading.value = false
    }
  },
)
</script>

<template>
  <n-drawer
    v-model:show="innerShow"
    :width="640"
  >
    <n-drawer-content closable>
      <template #header>
        <div class="flex items-center gap-[.5rem]">
          <span>用户详情</span>
          <n-tag
            v-if="user"
            size="small"
            :type="(userStatusMap[user.status] ?? { label: user.status, type: 'default' }).type"
            :bordered="false"
          >
            {{ (userStatusMap[user.status] ?? { label: user.status, type: 'default' }).label }}
          </n-tag>
        </div>
      </template>
      <n-spin :show="loading">
        <div
          v-if="detail && user"
          class="flex flex-col gap-[1rem]"
        >
          <!-- 基本信息 -->
          <section>
            <div class="drawer-subtitle">
              基本信息
            </div>
            <n-descriptions
              size="small"
              :column="1"
              bordered
              label-placement="left"
              label-class="w-[16rem]"
            >
              <n-descriptions-item label="用户名">
                {{ detail.username }}
              </n-descriptions-item>
              <n-descriptions-item label="邮箱">
                {{ detail.email ?? '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="手机号">
                {{ detail.phone ?? '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="角色">
                <div
                  v-if="detail.role_ids.length"
                  class="flex flex-wrap gap-[.4rem]"
                >
                  <n-tag
                    v-for="roleId in detail.role_ids"
                    :key="roleId"
                    size="small"
                    :bordered="false"
                  >
                    {{ roleNames.get(roleId) ?? `#${roleId}` }}
                  </n-tag>
                </div>
                <template v-else>—</template>
              </n-descriptions-item>
              <n-descriptions-item label="最后登录">
                {{ formatDateTime(detail.last_login_at) }}
              </n-descriptions-item>
              <n-descriptions-item label="创建时间">
                {{ formatDateTime(detail.created_at) }}
              </n-descriptions-item>
              <n-descriptions-item label="更新时间">
                {{ formatDateTime(detail.updated_at) }}
              </n-descriptions-item>
            </n-descriptions>
          </section>
        </div>
      </n-spin>
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
</style>
