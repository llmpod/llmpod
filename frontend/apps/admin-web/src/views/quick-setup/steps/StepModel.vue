<script setup lang="ts">
import { CloudUploadOutline } from '@vicons/ionicons5'
import { NEllipsis, NRadio, NTag } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { message, useDialog, useTable } from '@repo/hooks'
import { isSetupMock } from '@/api/quick-setup'
import AddModelDialog from '@/views/model-management/add-model/AddModelDialog.vue'
import {
  formatDateTime,
  SOURCE_LABEL,
  TYPE_TAG,
  typeLabel,
  useStore as useModelStore,
  type ModelRecord,
} from '@/views/model-management/useStore'
import { useStore } from '../useStore'

const { model, uploading, uploadProgress, applyModel, uploadModelFile } = useStore()
const modelStore = useModelStore()

/** 与模型管理页相同的添加模型向导;创建成功后回写向导选中的模型 */
function openAddModel() {
  useDialog({
    title: '添加模型',
    component: AddModelDialog,
    area: 60,
    btn: false,
    props: { onCreated: applyModel },
    dialogProps: { maskClosable: false },
  })
}

/** 模型表格:与模型管理一致的列,首列单选决定部署用模型 */
const { LmTable, LmPage, LmRefresh, refresh, total, loading } = useTable<ModelRecord>({
  request: modelStore.request,
  columns: [
    {
      title: '',
      key: 'select',
      width: 40,
      render: (row) => h(NRadio, { checked: model.value?.id === String(row.id) }),
    },
    { title: '名称', key: 'name', minWidth: 140 },
    {
      title: '来源',
      key: 'source',
      render: (row) =>
        h('div', { class: 'flex flex-col leading-tight' }, [
          h('span', row.source ? (SOURCE_LABEL[row.source] ?? row.source) : '—'),
          row.source_url
            ? h(NEllipsis, { class: 'text-base text-tertiary' }, { default: () => row.source_url })
            : null,
        ]),
    },
    {
      title: '类别',
      key: 'model_type',
      render: (row) => {
        const conf = TYPE_TAG[row.model_type]
        return h(
          NTag,
          {
            size: 'small',
            bordered: false,
            ...(conf ? { color: { color: conf.color, textColor: conf.textColor } } : {}),
          },
          { default: () => typeLabel(row.model_type) },
        )
      },
    },
    {
      title: '格式',
      key: 'format',
      render: (row) => row.format ?? '—',
    },
    {
      title: '大小',
      key: 'size_gb',
      render: (row) => (row.size_gb != null ? `${row.size_gb} GB` : '—'),
    },
    {
      title: '更新时间',
      key: 'updated_at',
      render: (row) => formatDateTime(row.updated_at),
    },
  ],
  rowKey: 'id',
  config: {
    pagination: { pageSize: 10 },
    request: { pageSizeField: 'page_size' },
    tableProps: {
      flexHeight: false,
      // 整行点击即选中(选中行高亮,radio 仅作选中态展示)
      rowProps: (row: ModelRecord) => ({
        class: 'cursor-pointer',
        onClick: () => applyModel(row),
      }),
      rowClassName: (row: ModelRecord) =>
        model.value?.id === String(row.id) ? 'model-row--selected' : '',
    },
  },
})

// 订阅模型 store 变更:向导内添加/外部增删改后自动刷新表格
const unsubscribe = modelStore.subscribe(() => {
  void refresh()
})
onBeforeUnmount(unsubscribe)

/** mock 演示:选择文件即开始上传(进度由 store 的 uploadProgress 驱动) */
function handleFileChange(options: { file: UploadFileInfo }) {
  const file = options.file.file
  if (!file) return
  uploadModelFile(file).catch(() => {
    message.error('模型上传失败,请重试')
  })
}

/** mock 演示:重新上传,清空已上传产物 */
function reupload() {
  model.value = null
}

/** 内嵌向导(AddModelDialog)暴露的驱动接口(模板 ref 经 proxyRefs 解包,current 为 number) */
interface EmbeddedWizardExpose {
  current: number
  handleInspect: () => Promise<void>
  submit: () => Promise<void>
  prev: () => void
}

/** 内嵌向导实例(空态直渲 AddModelDialog 时收集) */
const wizardRef = ref<EmbeddedWizardExpose | null>(null)

/** 空态内嵌向导模式(非 mock 且无模型):隐藏内部步骤条/按钮,由外层向导按钮驱动 */
const embeddedWizard = computed(() => !isSetupMock() && !loading.value && total.value === 0)

/** 外层「下一步」按钮文案:内嵌模式按内部步骤显示 探测模型源/确认提交,其余交由外层默认 */
const nextText = computed(() => {
  if (!embeddedWizard.value) return undefined
  return wizardRef.value?.current === 2 ? '确认提交' : '探测模型源'
})

/** 下一步按钮禁用态:内嵌模式由内部向导自行校验不禁用;否则未选中模型时不可点 */
const canNext = computed(() => (embeddedWizard.value ? true : !!model.value))

/** 「下一步」禁用提示:内嵌模式不禁用(无提示);其余提示先准备模型 */
const nextDisabledTip = computed(() => {
  if (embeddedWizard.value || model.value) return undefined
  return isSetupMock() ? '请先上传模型文件' : '请先添加并选中一个模型'
})

/** 下一步门控:内嵌模式驱动内部向导(探测/提交,不推进外层步骤);否则必须选中模型 */
async function validate(): Promise<boolean> {
  if (embeddedWizard.value) {
    const wizard = wizardRef.value
    if (!wizard) return false
    try {
      if (wizard.current === 1) await wizard.handleInspect()
      else await wizard.submit()
    }
    catch {
      // 探测/提交失败提示由 http 中间件统一给出,停留当前步
    }
    return false
  }
  if (model.value) return true
  message.warning('请先添加并选中一个模型,或选择「稍后在模型管理上传」')
  return false
}

/** 外层「上一步」钩子:内嵌向导处于步骤2 时先退回内部步骤1,返回 true 表示已消费不退出外层步骤 */
function prevAction(): boolean {
  if (embeddedWizard.value && wizardRef.value && wizardRef.value.current > 1) {
    wizardRef.value.prev()
    return true
  }
  return false
}

defineExpose({ canNext, nextDisabledTip, nextText, validate, prevAction })
</script>

<template>
  <div v-if="!isSetupMock()">
    <!-- 空态内嵌添加模型向导:限宽居中(大屏不拉满),隐藏内部步骤条与按钮,由外层向导按钮驱动 -->
    <div
      v-if="embeddedWizard"
      class="mx-auto w-full max-w-[76rem]"
    >
      <AddModelDialog
        ref="wizardRef"
        :on-created="applyModel"
        embedded
      />
    </div>
    <LmTableGroup v-else>
      <template #header-left>
        <n-button
          type="default"
          @click="openAddModel"
        >
          添加更多模型
        </n-button>
      </template>
      <template #header-right>
        <LmRefresh />
      </template>
      <template #content>
        <LmTable :max-height="320" />
      </template>
      <template #footer-right>
        <LmPage />
      </template>
    </LmTableGroup>
  </div>

  <!-- mock 演示:本地上传 -->
  <div v-else>
    <div
      v-if="model"
      class="flex flex-col items-center gap-[.8rem] py-[1.6rem]"
    >
      <n-alert
        type="success"
        :show-icon="false"
        class="w-full"
      >
        已上传:{{ model.name }}{{ model.sizeText ? `（${model.sizeText}）` : '' }}
      </n-alert>
      <n-button
        quaternary
        type="primary"
        @click="reupload"
      >
        重新上传
      </n-button>
    </div>
    <template v-else>
      <n-progress
        v-if="uploading"
        type="line"
        :percentage="uploadProgress"
        processing
        class="mb-[1.2rem]"
      />
      <n-upload
        :show-file-list="false"
        accept=".bin,.safetensors,.pt,.gguf"
        :disabled="uploading"
        @change="handleFileChange"
      >
        <n-upload-dragger>
          <div class="flex flex-col items-center gap-[.5rem] py-[1rem]">
            <n-icon
              :component="CloudUploadOutline"
              :size="40"
              class="text-tertiary"
            />
            <n-text>点击或拖拽模型文件到此区域</n-text>
            <n-text
              depth="3"
              class="text-small"
            >
              支持 .bin / .safetensors / .pt / .gguf,最大 50G
            </n-text>
          </div>
        </n-upload-dragger>
      </n-upload>
    </template>
  </div>
</template>

<style scoped lang="scss">
/* 选中行高亮(主题色 8% 底,跟随明暗/主题联动) */
:deep(.model-row--selected td) {
  background-color: rgba(var(--n-primary-color), 0.08);
}
</style>
