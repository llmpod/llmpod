<script setup lang="ts">
import {
  actionMap,
  actorTypeMap,
  formatDateTime,
  resourceTypeLabel,
  resultMap,
  type AuditLogData,
} from './useStore'

const props = defineProps<{
  show: boolean
  log: AuditLogData | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

/** 操作者展示名:用户名快照优先,缺失回退 #id,未知主体占位 */
const actorName = computed(() => {
  if (!props.log) return '—'
  if (props.log.actor_name) return props.log.actor_name
  return props.log.actor_id ? `#${props.log.actor_id}` : '未知主体'
})

/** 明细 JSON 美化展示;空值返回空串由模板占位 */
const detailJson = computed(() => {
  if (!props.log?.detail) return ''
  try {
    return JSON.stringify(props.log.detail, null, 2)
  } catch {
    return ''
  }
})
</script>

<template>
  <n-drawer v-model:show="innerShow" :width="640">
    <n-drawer-content closable>
      <template #header>
        <div class="flex items-center gap-[.5rem]">
          <span>审计事件详情</span>
          <n-tag v-if="log" size="small" :type="resultMap[log.result]?.type ?? 'default'" :bordered="false">
            {{ resultMap[log.result]?.label ?? log.result }}
          </n-tag>
        </div>
      </template>
      <div v-if="log" class="flex flex-col gap-[1rem]">
        <!-- 事件概览 -->
        <section>
          <div class="drawer-subtitle">事件概览</div>
          <n-descriptions
            size="small"
            :column="1"
            bordered
            label-placement="left"
            label-class="w-[10rem]"
          >
            <n-descriptions-item label="事件ID">
              {{ log.id }}
            </n-descriptions-item>
            <n-descriptions-item label="时间">
              {{ formatDateTime(log.created_at) }}
            </n-descriptions-item>
            <n-descriptions-item label="操作">
              <n-tag size="small" :type="actionMap[log.action]?.type ?? 'default'" :bordered="false">
                {{ actionMap[log.action]?.label ?? log.action }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="结果">
              <n-tag size="small" :type="resultMap[log.result]?.type ?? 'default'" :bordered="false">
                {{ resultMap[log.result]?.label ?? log.result }}
              </n-tag>
            </n-descriptions-item>
          </n-descriptions>
        </section>

        <!-- 操作者 -->
        <section>
          <div class="drawer-subtitle">操作者</div>
          <n-descriptions
            size="small"
            :column="1"
            bordered
            label-placement="left"
            label-class="w-[10rem]"
          >
            <n-descriptions-item label="用户名">
              {{ actorName }}
            </n-descriptions-item>
            <n-descriptions-item label="类型">
              {{ actorTypeMap[log.actor_type] ?? log.actor_type }}
            </n-descriptions-item>
            <n-descriptions-item label="操作者ID">
              {{ log.actor_id }}
            </n-descriptions-item>
            <n-descriptions-item label="IP地址">
              {{ log.ip_address ?? '—' }}
            </n-descriptions-item>
          </n-descriptions>
        </section>

        <!-- 资源信息 -->
        <section>
          <div class="drawer-subtitle">资源信息</div>
          <n-descriptions
            size="small"
            :column="1"
            bordered
            label-placement="left"
            label-class="w-[10rem]"
          >
            <n-descriptions-item label="资源类型">
              {{ resourceTypeLabel(log.resource_type) }}
            </n-descriptions-item>
            <n-descriptions-item label="资源ID">
              {{ log.resource_id ?? '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="归属租户">
              {{ log.tenant_id === null ? '全局' : `#${log.tenant_id}` }}
            </n-descriptions-item>
          </n-descriptions>
        </section>

        <!-- 明细(脱敏后参数/返回摘要/错误) -->
        <section>
          <div class="drawer-subtitle">明细</div>
          <pre
            v-if="detailJson"
            class="detail-json"
          >{{ detailJson }}</pre>
          <div v-else class="text-base text-secondary">
            无明细数据
          </div>
        </section>
      </div>
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

.detail-json {
  margin: 0;
  padding: 12px;
  border-radius: 8px;
  background: var(--c-fill-2);
  font-size: 12px;
  line-height: 1.6;
  font-family: var(--n-font-family-mono, monospace);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
