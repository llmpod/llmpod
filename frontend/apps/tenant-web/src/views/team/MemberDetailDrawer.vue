<script setup lang="ts">
import { memberStatusMap, useStore, formatDateTime, type Member } from './useStore'

const props = defineProps<{
  show: boolean
  member: Member | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const { getDetail, roleName } = useStore()
const detail = ref<Member>()
const loading = ref(false)
/** 竞态守卫:自增序号,迟到的旧响应直接丢弃 */
let detailSeq = 0

watch(
  () => props.show,
  async (visible) => {
    if (!visible || !props.member) return
    const seq = ++detailSeq
    // 先清空旧成员数据,避免 loading 期展示上一成员
    detail.value = undefined
    loading.value = true
    try {
      const result = await getDetail(props.member.id)
      if (seq !== detailSeq) return
      detail.value = result
    } finally {
      if (seq === detailSeq) loading.value = false
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
          <span>成员详情</span>
          <n-tag
            v-if="member"
            size="small"
            :type="(memberStatusMap[member.status] ?? { label: member.status, type: 'default' }).type"
            :bordered="false"
          >
            {{ (memberStatusMap[member.status] ?? { label: member.status, type: 'default' }).label }}
          </n-tag>
        </div>
      </template>
      <n-spin :show="loading">
        <div
          v-if="detail && member"
          class="flex flex-col gap-[1rem]"
        >
          <!-- 基本信息 -->
          <section>
            <div class="mb-[.5rem] text-sm font-semibold text-strong">
              基本信息
            </div>
            <n-descriptions
              size="small"
              :column="1"
              bordered
              label-placement="left"
              label-class="w-[10rem]"
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
                  class="flex flex-wrap gap-[.5rem]"
                >
                  <n-tag
                    v-for="id in detail.role_ids"
                    :key="id"
                    size="small"
                    type="primary"
                    :bordered="false"
                  >
                    {{ roleName(id) }}
                  </n-tag>
                </div>
                <span v-else>—</span>
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
