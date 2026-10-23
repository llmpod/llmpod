<script setup lang="ts">
import { CloseOutline, CloudUploadOutline, DocumentOutline } from '@vicons/ionicons5'
import type { UploadFileInfo, UploadInst } from 'naive-ui'
import { FormInput, FormRadioButtonGroup, useForm, type UseFormReturn } from '@repo/hooks'
import { SOURCE_OPTIONS, SOURCE_URL_PLACEHOLDER, type ModelFiles } from '../useStore'
import type { ManifestWarning, ManifestWarningSeverity } from '@/api/model'
import type { LocalMode, SourceFormValues } from './useWizard'
import type { UploadState } from './useUpload'

/** 本地模型获取方式选项(local 源二级选择):path=manager 本机已有路径;upload=浏览器选文件夹分片上传 */
const LOCAL_MODE_OPTIONS: { label: string; value: LocalMode }[] = [
  { label: '本机路径', value: 'path' },
  { label: '上传文件', value: 'upload' },
]

const props = defineProps<{
  /** 最近一次探测结果:仅用于展示软失败原因 */
  inspected: ModelFiles | null
  /** 分片上传状态(source=local 时展示进度) */
  uploadState: UploadState
  /** 最近一次预检警告:清单中命中的文件按级别着色 */
  manifestWarnings: ManifestWarning[]
}>()

/** 文件清单增删后旧的预检标记即失效,通知父级清除 */
const emit = defineEmits<{ filesChange: [] }>()

/** 分片上传中:禁用上方表单项(切换源/获取方式会重排上传区,来源地址也在回填流程中) */
const uploading = computed(() => props.uploadState.phase !== 'idle')

// 显式标注类型:options 闭包内引用 form.params() 做占位/显隐联动,避免循环推断报错
const form: UseFormReturn<SourceFormValues> = useForm<SourceFormValues>({
  data: () => ({
    source: 'huggingface',
    source_url: '',
    revision: 'main',
    local_mode: 'path',
  }),
  options: () => [
    FormRadioButtonGroup('模型源', 'source', SOURCE_OPTIONS, { disabled: uploading }),
    FormRadioButtonGroup('获取方式', 'local_mode', LOCAL_MODE_OPTIONS, {
      disabled: uploading,
      // 仅 local 源需要区分「本机路径 / 上传文件」
      visible: computed(() => form.params().source === 'local'),
    }),
    FormInput('来源地址', 'source_url', {
      disabled: uploading,
      rules: [{ required: true, message: '请输入来源地址', trigger: 'blur' }],
      // local+上传模式改为文件选择,来源地址由上传完成后的 staged 路径回填,不展示输入框
      visible: computed(
        () => form.params().source !== 'local' || form.params().local_mode === 'path',
      ),
      attrs: { placeholder: SOURCE_URL_PLACEHOLDER[form.params().source] },
    }),
    FormInput('版本(revision)', 'revision', {
      disabled: uploading,
      // revision 仅 HF 源有意义
      visible: computed(() => form.params().source === 'huggingface'),
      attrs: { placeholder: 'commit-hash,默认 main' },
    }),
  ],
  layout: { labelPlacement: 'top' },
})

const { LmForm, validate, params } = form

/** 已选本地文件(n-upload 受控列表;default-upload=false 仅收集不上传,点「探测模型源」时才上传) */
const fileList = ref<UploadFileInfo[]>([])

// 清单增删后通知父级清除预检标记
watch(fileList, () => emit('filesChange'))

/** 预检级别 → 文件名着色(fatal 最醒目) */
const SEVERITY_TEXT_CLASS: Record<ManifestWarningSeverity, string> = {
  fatal: 'text-error',
  warn: 'text-warning',
  info: 'text-info',
}

/** 各文件最高级别预检警告(file → warning,fatal > warn > info),清单行着色与原因提示依据 */
const warningByFile = computed(() => {
  const order: ManifestWarningSeverity[] = ['fatal', 'warn', 'info']
  const map = new Map<string, ManifestWarning>()
  for (const warning of props.manifestWarnings) {
    if (!warning.file) continue
    const prev = map.get(warning.file)
    if (!prev || order.indexOf(warning.severity) < order.indexOf(prev.severity)) {
      map.set(warning.file, warning)
    }
  }
  return map
})

/** n-upload 实例:已有文件时清单区的「继续添加」按钮通过它打开文件夹选择框 */
const uploadRef = ref<UploadInst | null>(null)

/** 所选文件夹名称(目录选择/拖拽时从相对路径首段解析),作为 declare 的 staged 目录名 */
const folderName = computed(() => {
  for (const item of fileList.value) {
    const relativePath = item.file?.webkitRelativePath || item.fullPath
    const top = relativePath?.split('/').filter(Boolean)[0]
    if (top) return top
  }
  return null
})

/** 文件大小格式化(B/KB/MB/GB) */
function formatSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

/** 已选文件总大小 */
const totalSize = computed(() =>
  fileList.value.reduce((sum, item) => sum + (item.file?.size ?? 0), 0),
)

/** 移除单个已选文件(仅影响待上传清单,不触发请求) */
function removeFile(id: string): void {
  fileList.value = fileList.value.filter((item) => item.id !== id)
}

// 向导父级据此校验并读取表单值;files 供 local 源分片上传,folderName 作 declare 目录名
defineExpose({
  validate,
  params,
  files: () => fileList.value.map((item) => item.file!).filter(Boolean),
  folderName: () => folderName.value,
})
</script>

<template>
  <div class="flex flex-col gap-[.25rem]">
    <LmForm />
    <!-- local 源且选「上传文件」:选择本地模型文件夹,点「探测模型源」时先分片上传再探测;
         选「本机路径」时上方「来源地址」输入 manager 本机路径直接探测,无需上传 -->
    <div
      v-if="params().source === 'local' && params().local_mode === 'upload'"
      class="flex flex-col gap-[.5rem]"
    >
      <n-text>模型文件</n-text>
      <template v-if="uploading">
        <n-progress
          type="line"
          :percentage="uploadState.percentage"
          processing
          indicator-placement="inside"
        />
      </template>
      <n-upload
        ref="uploadRef"
        v-model:file-list="fileList"
        multiple
        directory
        directory-dnd
        :default-upload="false"
        :disabled="uploading"
        :show-file-list="false"
      >
        <n-upload-dragger v-if="!fileList.length">
          <div class="flex flex-col items-center gap-[.5rem] py-[1rem]">
            <n-icon :component="CloudUploadOutline" :size="40" class="text-tertiary" />
            <n-text>点击选择模型文件夹,或拖拽模型文件夹到此区域</n-text>
            <n-text depth="3" class="text-small">
              请包含模型目录下的全部文件(权重 + config 等),确认后点击「探测模型源」开始上传
            </n-text>
          </div>
        </n-upload-dragger>
        <!-- 已有文件:拖拽区与文件清单合并(隐藏 n-upload 默认列表),整片清单仍可继续拖入文件;
             清单内点击不弹选择框(@click.stop),由「继续添加」按钮显式打开 -->
        <n-upload-dragger v-else style="padding: 0">
          <div class="bg-fill-1" @click.stop>
            <div
              class="flex items-center justify-between gap-[.8rem] px-[1.2rem] py-[.6rem] border-b border-line"
            >
              <n-text class="text-small">
                已选 {{ fileList.length }} 个文件 · 共 {{ formatSize(totalSize) }}
              </n-text>
              <div class="flex items-center gap-[1.2rem]">
                <n-button
                  text
                  type="primary"
                  size="small"
                  :disabled="uploading"
                  @click="uploadRef?.openOpenFileDialog()"
                >
                  继续添加
                </n-button>
                <n-button text size="small" :disabled="uploading" @click="fileList = []">
                  清空
                </n-button>
              </div>
            </div>
            <n-scrollbar class="max-h-[18rem] min-h-[18rem]">
              <div
                v-for="item in fileList"
                :key="item.id"
                class="flex items-center gap-[.8rem] px-[1.2rem] py-[.4rem] hover:bg-fill-2"
              >
                <n-icon
                  :component="DocumentOutline"
                  class="shrink-0"
                  :class="
                    warningByFile.get(item.name)
                      ? SEVERITY_TEXT_CLASS[warningByFile.get(item.name)!.severity]
                      : 'text-tertiary'
                  "
                />
                <span
                  class="flex-1 truncate text-small text-left"
                  :class="
                    warningByFile.get(item.name)
                      ? SEVERITY_TEXT_CLASS[warningByFile.get(item.name)!.severity]
                      : ''
                  "
                  :title="warningByFile.get(item.name)?.message ?? item.fullPath ?? item.name"
                >
                  {{ item.name }}
                </span>
                <!-- 上传中:行级进度条 + 百分比(窄条,避免挤压文件名);空闲:文件大小 -->
                <template v-if="uploading">
                  <n-progress
                    type="line"
                    :percentage="uploadState.filePercentages[item.name] ?? 0"
                    :show-indicator="false"
                    :height="4"
                    processing
                    class="!w-[8rem] shrink-0"
                  />
                  <n-text depth="3" class="text-small shrink-0 tabular-nums text-right">
                    {{ uploadState.filePercentages[item.name] ?? 0 }}%
                  </n-text>
                </template>
                <n-text v-else depth="3" class="text-small shrink-0 tabular-nums">
                  {{ formatSize(item.file?.size ?? 0) }}
                </n-text>
                <n-button
                  quaternary
                  circle
                  size="tiny"
                  :disabled="uploading"
                  aria-label="移除文件"
                  @click="removeFile(item.id)"
                >
                  <template #icon>
                    <n-icon :component="CloseOutline" />
                  </template>
                </n-button>
              </div>
            </n-scrollbar>
          </div>
        </n-upload-dragger>
      </n-upload>
    </div>
    <n-alert v-if="inspected && !inspected.success" type="warning" :show-icon="false">
      {{ inspected.message || '探测失败,请检查来源地址后重试' }}
    </n-alert>
  </div>
</template>

<style scoped>
/*
 * naive-ui 的 dragger-inside 修正类是在 render 期间写入的非响应式标记,要等下一次渲染才生效;
 * 带文件切换模型源再切回时 n-upload 重挂载后不再重渲染,trigger 保持 inline-block,
 * 清单宽度收缩为内容宽度。这里强制 trigger 块级,不依赖该修正类
 */
:deep(.n-upload-trigger) {
  display: block;
}
</style>
