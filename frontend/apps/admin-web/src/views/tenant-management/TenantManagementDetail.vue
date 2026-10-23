<script setup lang="ts">
import { NSpin, NTag, NButton, NDescriptions, NDescriptionsItem } from 'naive-ui'
import { useDialog, useDialogClose, useConfirm, message } from '@repo/hooks'
import TenantManagementForm from './TenantManagementForm.vue'
import TenantWalletAdjustForm from './TenantWalletAdjustForm.vue'
import {
  useStore,
  tenantStatusMap,
  formatDateTime,
  formatDecimal,
  type Tenant,
} from './useStore'
import { brandIconOf, modelTypeMap } from '@/views/model-mark/useStore'

const props = defineProps<{
  tenant: Tenant
}>()

const close = useDialogClose()
const { getDetail, remove, toggleStatus, subscribe } = useStore()

/** 操作按钮图标:public/icons/tenant 下的 svg,mask 渲染为可着色图标,跟随按钮文字色 */
function iconMask(name: string) {
  const src = `${import.meta.env.BASE_URL}icons/tenant/${name}.svg`
  return {
    backgroundColor: 'currentColor',
    mask: `url(${src}) center/contain no-repeat`,
    WebkitMask: `url(${src}) center/contain no-repeat`,
  }
}

const detail = ref<Tenant>()
const loading = ref(true)

async function loadDetail() {
  detail.value = await getDetail(props.tenant.id)
}

onMounted(async () => {
  try {
    await loadDetail()
  }
  finally {
    loading.value = false
  }
})

/** 订阅变更:信息变更后自动刷新详情 */
const unsubscribe = subscribe(() => {
  void loadDetail()
})
onBeforeUnmount(unsubscribe)

/** 编辑:复用表单弹窗 */
function onEdit() {
  useDialog({
    title: '编辑租户',
    component: TenantManagementForm,
    area: 40,
    props: { initial: detail.value ? { ...detail.value } : { ...props.tenant } },
    btn: { confirmText: '保存', cancelText: '取消' },
    dialogProps: { maskClosable: false },
  })
}

/** 调额:人工 add/subtract/override 租户余额(成功后经 subscribe 刷新详情) */
function onAdjust() {
  const tenant = detail.value ?? props.tenant
  useDialog({
    title: `调额 - ${tenant.name}`,
    component: TenantWalletAdjustForm,
    area: 40,
    props: { tenant },
    btn: { confirmText: '确认调额', cancelText: '取消' },
    dialogProps: { maskClosable: false },
  })
}

/** 禁用/启用 */
async function onToggleStatus() {
  if (!detail.value) return
  const status = await toggleStatus(detail.value)
  message.success(status === 'disabled' ? `已禁用:${detail.value.name}` : `已启用:${detail.value.name}`)
}

/** 已封禁模型(纯展示;调整入口在编辑表单的「模型封禁」项) */
const blockedModels = computed(() => detail.value?.blocked_models ?? [])

/** 模型类别文案:未知类别回退原始字符串(modelTypeMap 来自模型市场,键为受控 ModelType) */
function modelTypeText(type: string): string {
  return modelTypeMap[type as keyof typeof modelTypeMap] ?? type
}

/** 删除:确认后删除并关闭详情(后端级联清空该租户用户及其角色) */
async function onDelete() {
  try {
    await useConfirm({
      title: '删除租户',
      content: `确定删除租户「${props.tenant.name}」吗?该租户下的用户及其角色将被一并清空,删除后不可恢复。`,
      confirmText: '删除',
    })
  }
  catch {
    return
  }
  await remove(props.tenant.id)
  message.success(`已删除:${props.tenant.name}`)
  close()
}
</script>

<template>
  <n-spin :show="loading">
    <div
      v-if="detail"
      class="tenant-detail"
    >
      <!-- 顶部操作 -->
      <div class="detail-actions">
        <n-button
          size="small"
          @click="onEdit"
        >
          <template #icon>
            <span
              class="action-icon"
              :style="iconMask('edit')"
            />
          </template>
          编辑
        </n-button>
        <n-button
          size="small"
          @click="onAdjust"
        >
          <template #icon>
            <span
              class="action-icon"
              :style="iconMask('edit')"
            />
          </template>
          调额
        </n-button>
        <n-button
          size="small"
          @click="onToggleStatus"
        >
          <template #icon>
            <span
              class="action-icon"
              :style="iconMask('deny')"
            />
          </template>
          {{ detail.status === 'active' ? '禁用' : '启用' }}
        </n-button>
        <n-button
          size="small"
          type="error"
          ghost
          @click="onDelete"
        >
          <template #icon>
            <span
              class="action-icon"
              :style="iconMask('delete')"
            />
          </template>
          删除
        </n-button>
      </div>

      <!-- 基本信息 -->
      <LmCard
        size="small"
        title="基本信息"
        class="panel"
      >
        <n-descriptions
          size="small"
          :column="1"
          label-placement="left"
          bordered
        >
          <n-descriptions-item label="名称">
            {{ detail.name }}
          </n-descriptions-item>
          <n-descriptions-item label="状态">
            <n-tag
              size="small"
              :type="(tenantStatusMap[detail.status] ?? { label: detail.status, type: 'default' }).type"
              :bordered="false"
            >
              {{ (tenantStatusMap[detail.status] ?? { label: detail.status, type: 'default' }).label }}
            </n-tag>
          </n-descriptions-item>
          <n-descriptions-item label="总余额(元)">
            {{ formatDecimal(detail.balance) }}
          </n-descriptions-item>
          <n-descriptions-item label="可用余额(元)">
            {{ formatDecimal(detail.available) }}
          </n-descriptions-item>
          <n-descriptions-item label="创建时间">
            {{ formatDateTime(detail.created_at) }}
          </n-descriptions-item>
          <n-descriptions-item label="更新时间">
            {{ formatDateTime(detail.updated_at) }}
          </n-descriptions-item>
        </n-descriptions>
      </LmCard>

      <!-- 模型封禁(纯展示;调整入口在编辑表单的「模型封禁」项) -->
      <LmCard
        size="small"
        title="模型封禁"
        class="panel"
      >
        <div class="flex flex-col gap-[.75rem]">
          <template v-if="blockedModels.length">
            <div class="text-secondary text-base">
              以下 {{ blockedModels.length }} 个模型已被封禁,该租户无法调用;其余已发布模型默认放行
            </div>
            <div class="grid grid-cols-1 gap-[.5rem] xl:grid-cols-2">
              <div
                v-for="model in blockedModels"
                :key="model.route_id"
                class="flex max-w-full items-center gap-[.75rem] rounded-lg border border-dashed border-line bg-fill-2 py-[.5rem] pr-[.75rem] pl-[.5rem]"
              >
                <div class="flex h-[2.6rem] w-[2.6rem] shrink-0 items-center justify-center rounded-lg bg-fill-1 grayscale">
                  <n-icon
                    :size="24"
                    class="text-tertiary"
                  >
                    <component :is="brandIconOf(model.name)" />
                  </n-icon>
                </div>
                <div class="flex min-w-0 flex-col gap-[.2rem]">
                  <span
                    class="truncate text-small text-secondary font-mono"
                    :title="model.name"
                  >
                    {{ model.name }}
                  </span>
                  <span class="text-base text-disabled">
                    {{ modelTypeText(model.model_type) }}
                  </span>
                </div>
                <n-tag
                  size="small"
                  type="error"
                  :bordered="false"
                  class="ml-auto shrink-0"
                >
                  已封禁
                </n-tag>
              </div>
            </div>
          </template>
          <div
            v-else
            class="text-secondary text-base"
          >
            未封禁任何模型,全部已发布模型可用
          </div>
        </div>
      </LmCard>
    </div>
    <div
      v-else
      class="tenant-detail tenant-detail--loading"
    />
  </n-spin>
</template>

<style scoped lang="scss">
.tenant-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;

  &--loading {
    min-height: 320px;
  }
}

.detail-actions {
  display: flex;
  gap: 8px;
}

.action-icon {
  width: 14px;
  height: 14px;
  display: inline-block;
}

.panel {
  :deep(.n-card__content) {
    padding: 12px 16px;
  }
}
</style>
