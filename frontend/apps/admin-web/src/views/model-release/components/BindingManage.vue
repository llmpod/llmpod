<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NInputNumber, NSwitch } from 'naive-ui'
import { useConfirm, useDialog, message } from '@repo/hooks'
import type { SelectOption } from 'naive-ui'
import { listEndpoints, type EndpointData } from '@/api/endpoint'
import EndpointForm from './endpoint/EndpointForm.vue'
import { useStore as useEndpointStore } from './endpoint/useStore'
import {
  useStore,
  type RouteBindingData,
  type PublishData,
} from '../useStore'

const props = defineProps<{
  show: boolean
  /** 当前操作的路由（null=未选） */
  publish: PublishData | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const { upsertBindingItem, updateBindingItem, toggleBindingItem, removeBindingItem } = useStore()
/** 端点 store:订阅增删改,在抽屉内创建端点后即时刷新选项 */
const { subscribe: subscribeEndpoints } = useEndpointStore()

/** 抽屉内本地绑定列表:打开时取快照,添加/移除后即时更新(不等父级刷新) */
const localBindings = ref<RouteBindingData[]>([])

/** 端点列表（供添加供应商选择） */
const endpointOptions = ref<SelectOption[]>([])
/** 端点 ID → 端点信息映射 */
const endpointMap = ref<Map<number, EndpointData>>(new Map())

/** 端点显示名(名称+协议,映射不到时退回 #id) */
function bindingLabel(b: RouteBindingData): string {
  const ep = endpointMap.value.get(b.endpoint_id)
  return ep ? `${ep.name}（${ep.protocol.toUpperCase()}）` : `#${b.endpoint_id}`
}

/** 用接口返回的绑定就地替换本地行(按 endpoint_id 匹配,不存在则追加) */
function replaceLocalBinding(binding: RouteBindingData) {
  const idx = localBindings.value.findIndex((x) => x.endpoint_id === binding.endpoint_id)
  if (idx >= 0) localBindings.value.splice(idx, 1, binding)
  else localBindings.value.push(binding)
}

/** 端点选项是否已加载（进入抽屉后按需加载一次） */
const endpointsLoaded = ref(false)

/** 行内编辑态:当前编辑行的 endpoint_id(一次仅一行)与编辑中的值 */
const editingEndpointId = ref<number | null>(null)
const editWeight = ref<number | null>(null)
const editPriority = ref<number | null>(null)
/** 保存请求中(防重复提交) */
const saving = ref(false)

/** 启停切换中的行:请求期间 switch 置 loading,防重复点击 */
const togglingIds = ref(new Set<number>())

/** 拉取端点列表并刷新选项与映射 */
async function loadEndpointOptions() {
  try {
    const list = await listEndpoints()
    endpointOptions.value = list.map((e: EndpointData) => ({
      label: `${e.name}（${e.served_model} · ${e.protocol.toUpperCase()}）`,
      value: e.id,
    }))
    endpointMap.value = new Map(list.map((e: EndpointData) => [e.id, e]))
  } catch {
    // 忽略,选择框为空
  } finally {
    endpointsLoaded.value = true
  }
}

watch(
  () => props.show,
  (visible, _prev, onCleanup) => {
    if (!visible) return
    // 打开时取当前路由的绑定快照,并清掉上次遗留的行内编辑态
    localBindings.value = [...(props.publish?.bindings ?? [])]
    editingEndpointId.value = null
    // 端点选项按需加载（一次）
    if (!endpointsLoaded.value) void loadEndpointOptions()
    // 弹窗内创建端点成功(store notify)后自动刷新选项,新端点立即可选;
    // 抽屉关闭或组件卸载时经 onCleanup 取消订阅
    onCleanup(subscribeEndpoints(() => void loadEndpointOptions()))
  },
)

/** 可添加的端点:过滤已挂载供应商(已挂载的调整走移除后重加) */
const availableEndpointOptions = computed(() => {
  const bound = new Set(localBindings.value.map((b) => b.endpoint_id))
  return endpointOptions.value.filter((o) => !bound.has(o.value as number))
})

/** 添加供应商表单 */
const selectedEndpointId = ref<number | null>(null)
const weight = ref<number | null>(1)
const priority = ref<number | null>(0)
const adding = ref(false)

async function onAdd() {
  if (!props.publish) return
  if (!selectedEndpointId.value) {
    message.warning('请选择来源端点')
    return
  }
  adding.value = true
  try {
    const binding = await upsertBindingItem(props.publish.route.id, {
      endpoint_id: selectedEndpointId.value,
      weight: weight.value,
      priority: priority.value,
      enabled: true,
    })
    // 本地即时更新:同 endpoint 覆盖,否则追加
    replaceLocalBinding(binding)
    message.success('已添加供应商')
    selectedEndpointId.value = null
    weight.value = 1
    priority.value = 0
  } catch {
    // 错误已由 HTTP 层提示
  } finally {
    adding.value = false
  }
}

/** 进入行内编辑:回填当前权重/优先级 */
function onEdit(b: RouteBindingData) {
  editingEndpointId.value = b.endpoint_id
  editWeight.value = b.weight
  editPriority.value = b.priority
}

/** 保存行内编辑的权重/优先级 */
async function onSave(b: RouteBindingData) {
  if (!props.publish) return
  if (editWeight.value == null) {
    message.warning('请填写权重（≥1）')
    return
  }
  saving.value = true
  try {
    const binding = await updateBindingItem(props.publish.route.id, b.endpoint_id, {
      weight: editWeight.value,
      ...(editPriority.value != null ? { priority: editPriority.value } : {}),
    })
    replaceLocalBinding(binding)
    message.success('已保存')
    editingEndpointId.value = null
  } catch {
    // 错误已由 HTTP 层提示,保留编辑态便于修正
  } finally {
    saving.value = false
  }
}

/** 切换启用状态(禁用=临时摘除,启用=恢复) */
async function onToggleEnabled(b: RouteBindingData) {
  if (!props.publish) return
  if (togglingIds.value.has(b.endpoint_id)) return
  togglingIds.value.add(b.endpoint_id)
  try {
    const binding = await toggleBindingItem(props.publish.route.id, b)
    replaceLocalBinding(binding)
    message.success(binding.enabled ? `已启用：${bindingLabel(b)}` : `已禁用：${bindingLabel(b)}`)
  } catch {
    // 失败不更新本地行,switch 随行数据自动回弹
  } finally {
    togglingIds.value.delete(b.endpoint_id)
  }
}

/** 移除供应商 */
async function onRemove(binding: RouteBindingData) {
  if (!props.publish) return
  try {
    await useConfirm({
      title: '移除供应商',
      content: `确定从路由中移除「${bindingLabel(binding)}」吗？移除后该端点不再承接此模型的调用。`,
      confirmText: '移除',
      cancelText: '取消',
    })
    await removeBindingItem(props.publish.route.id, binding.endpoint_id)
    localBindings.value = localBindings.value.filter((b) => b.endpoint_id !== binding.endpoint_id)
    message.success('已移除供应商')
  } catch {
    // 取消
  }
}

/** 直接打开「接入端点」弹窗(与 EndpointManage.openCreate 同模式);创建成功经 store 订阅刷新选项 */
function openEndpointCreate() {
  useDialog({
    title: '接入端点',
    component: EndpointForm,
    area: 48,
    btn: { confirmText: '创建', cancelText: '取消' },
    dialogProps: { maskClosable: false },
  })
}
</script>

<template>
  <n-drawer v-model:show="innerShow" :width="560">
    <n-drawer-content closable>
      <template #header>
        <span>供应商 - {{ publish?.route.name ?? '' }}</span>
      </template>

      <div class="flex flex-col gap-[1.25rem]">
        <!-- 已挂载供应商列表 -->
        <div class="flex flex-col gap-[.5rem]">
          <span class="text-small font-medium">已挂载供应商（{{ localBindings.length }}）</span>
          <n-data-table
            v-if="localBindings.length"
            :data="localBindings"
            :columns="[
              {
                title: '来源端点',
                key: 'endpoint_id',
                render: (b) => bindingLabel(b),
              },
              {
                title: '权重',
                key: 'weight',
                width: 90,
                render: (b) =>
                  editingEndpointId === b.endpoint_id
                    ? h(NInputNumber, {
                        value: editWeight,
                        min: 1,
                        size: 'small',
                        showButton: false,
                        'onUpdate:value': (value) => {
                          editWeight = value
                        },
                      })
                    : b.weight,
              },
              {
                title: '优先级',
                key: 'priority',
                width: 90,
                render: (b) =>
                  editingEndpointId === b.endpoint_id
                    ? h(NInputNumber, {
                        value: editPriority,
                        size: 'small',
                        showButton: false,
                        'onUpdate:value': (value) => {
                          editPriority = value
                        },
                      })
                    : b.priority,
              },
              {
                title: '启用',
                key: 'enabled',
                width: 60,
                render: (b) =>
                  h(NSwitch, {
                    value: b.enabled,
                    size: 'small',
                    loading: togglingIds.has(b.endpoint_id),
                    'onUpdate:value': () => onToggleEnabled(b),
                  }),
              },
              {
                title: '操作',
                key: 'actions',
                width: 90,
                render: (b) => {
                  const editing = editingEndpointId === b.endpoint_id
                  return h('div', { class: 'flex items-center gap-[.75rem]' }, [
                    h(
                      NButton,
                      {
                        text: true,
                        type: 'primary',
                        size: 'small',
                        loading: saving && editing,
                        onClick: () => (editing ? onSave(b) : onEdit(b)),
                      },
                      { default: () => (editing ? '保存' : '编辑') },
                    ),
                    h(
                      NButton,
                      { text: true, type: 'error', size: 'small', onClick: () => onRemove(b) },
                      { default: () => '移除' },
                    ),
                  ])
                },
              },
            ]"
            :bordered="false"
            size="small"
          />
          <span v-else class="text-small text-secondary">
            暂无供应商，路由还没有任何来源端点承接调用
          </span>
        </div>

        <!-- 添加供应商表单 -->
        <div class="flex flex-col gap-[.75rem]">
          <span class="text-small font-medium">添加供应商</span>

          <template v-if="availableEndpointOptions.length || !endpointsLoaded">
            <div class="flex flex-col gap-[.25rem]">
              <span class="text-small">来源端点</span>
              <LmSelect
                v-model:value="selectedEndpointId"
                :options="availableEndpointOptions"
                :loading="!endpointsLoaded"
                placeholder="选择要挂到该路由的来源端点"
                filterable
                clearable
              />
            </div>

            <div class="flex flex-col gap-[.25rem]">
              <span class="text-small">权重（默认 1）</span>
              <n-input-number
                v-model:value="weight"
                :min="1"
                placeholder="同优先级下按权重加权随机分流"
                class="w-full"
              />
              <span class="text-small text-secondary">
                同优先级的供应商之间，按权重比例随机分配请求
              </span>
            </div>

            <div class="flex flex-col gap-[.25rem]">
              <span class="text-small">优先级（默认 0）</span>
              <n-input-number
                v-model:value="priority"
                placeholder="数值大的优先承接"
                class="w-full"
              />
              <span class="text-small text-secondary">
                优先级大的供应商优先承接；其故障后按优先级依次降级
              </span>
            </div>

            <div class="flex justify-end">
              <NButton type="primary" :loading="adding" @click="onAdd">添加供应商</NButton>
            </div>
          </template>

          <span v-else-if="endpointsLoaded" class="text-base text-secondary">
            没有可添加的端点了（全部已挂载）。如需新增来源，请先
            <n-button text type="primary" size="small" @click="openEndpointCreate">
              添加端点
            </n-button>
          </span>
        </div>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>
