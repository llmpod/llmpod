import { message, useConfirm, useDialogClose } from '@repo/hooks'
import { validateManifest, type ManifestWarning } from '@/api/model'
import {
  MODEL_TYPE_OPTIONS,
  useStore,
  type FeasibilityReport,
  type InspectModelPayload,
  type ModelData,
  type ModelFiles,
  type ModelSource,
} from '../useStore'
import { useChunkedUpload } from './useUpload'

/** 本地模型获取方式:upload=浏览器选文件夹分片上传;path=manager 本机已有路径直接探测 */
export type LocalMode = 'upload' | 'path'

/** 步骤 1:模型源表单值(local_mode 仅前端交互用,探测/创建时按模式取 source_url,不下发) */
export interface SourceFormValues {
  source: ModelSource
  source_url: string
  revision: string
  local_mode: LocalMode
}

/** 步骤 2:模型信息表单值(format/architecture/size_gb 由探测回填,仅展示不编辑) */
export interface InfoFormValues {
  name: string
  model_type: string
  format: string | null
  architecture: string
  size_gb: number | null
  quantization: string | null
  is_moe: boolean
}

/** 步骤表单组件 expose 契约:父级向导据此校验并读取值 */
export interface StepFormExpose<T extends object> {
  validate: () => Promise<void>
  params: () => T
}

/** 步骤 1 expose:额外提供本地文件清单(source=local 时探测前先分片上传) */
export interface SourceStepExpose extends StepFormExpose<SourceFormValues> {
  files: () => File[]
  /** 所选文件夹名称(declare 的 staged 目录名;无法解析时为 null) */
  folderName: () => string | null
}

/**
 * 添加模型向导编排:步骤切换、探测、提交。
 * 表单状态分散在各 Step 组件内部(useForm 自治),此处通过模板 ref 收集 expose 驱动流程。
 * onCreated:创建成功回调(quick-setup 向导复用本弹窗时回写新建模型,可选)。
 */
export function useWizard(onCreated?: (record: ModelData) => void) {
  const close = useDialogClose()
  const { create, inspect } = useStore()

  /** 向导步骤 */
  const STEPS = ['模型源', '模型信息'] as const

  /** 当前步骤(1 起) */
  const current = ref(1)

  /** 步骤组件 expose(模板 ref 收集) */
  const sourceStep = ref<SourceStepExpose | null>(null)
  const infoStep = ref<StepFormExpose<InfoFormValues> | null>(null)

  /** local 源分片上传编排(进度经 uploadState 透传给 StepSource 展示) */
  const upload = useChunkedUpload()

  // 弹窗关闭(取消/遮罩/提交成功等所有路径都会卸载组件)时中止未完成的上传会话
  onUnmounted(() => upload.abort())

  /** 探测中状态(防重复点击) */
  const inspecting = ref(false)
  /** 最近一次探测结果;探测成功(success=true)才能进入下一步 */
  const inspected = ref<ModelFiles | null>(null)
  /** 可部署性检测报告(best-effort,可能为 null);deployable=false 时展示 warning */
  const feasibility = ref<FeasibilityReport | null>(null)

  /** 已通过预检的文件清单签名(与 useUpload 一致:name:size 排序拼接;同组文件不重复确认) */
  let manifestCheckedKey = ''

  /** 最近一次预检警告;StepSource 据此在文件清单中高亮问题文件 */
  const manifestWarnings = ref<ManifestWarning[]>([])

  /** 渲染预检警告列表(useConfirm content) */
  function renderWarnings(warnings: ManifestWarning[]) {
    return h('div', [
      h('p', { style: 'margin:0;' }, '检测到以下问题,确认继续上传?'),
      h(
        'ul',
        { style: 'margin:0.6rem 0 0;padding-left:1.6rem;line-height:1.8;' },
        warnings.map((warning) =>
          h('li', warning.file ? `${warning.file}:${warning.message}` : warning.message),
        ),
      ),
    ])
  }

  /**
   * 上传前预检(validate-manifest,文件清单未落盘):
   * fatal → 提示并阻止;warn/info → 弹确认,用户同意才继续(取消抛 ConfirmCancelError)。
   * 接口失败抛错(提示由 http 中间件统一给出),调用方 catch 后中断即可。
   */
  async function ensureManifestChecked(files: File[]): Promise<void> {
    const key = files
      .map((file) => `${file.name}:${file.size}`)
      .sort()
      .join('\n')
    if (key === manifestCheckedKey) return
    const { warnings } = await validateManifest({
      files: files.map((file) => ({ name: file.name, size: file.size })),
    })
    manifestWarnings.value = warnings
    const fatals = warnings.filter((warning) => warning.severity === 'fatal')
    if (fatals.length) {
      // fatal 需用户看完并手动关闭:不自动消失,展示关闭按钮
      message.error(
        `模型文件预检未通过:${fatals
          .map((warning) => (warning.file ? `${warning.file}:${warning.message}` : warning.message))
          .join(';')}`,
        { duration: null, closable: true },
      )
      throw new Error('manifest validation failed')
    }
    const rest = warnings.filter((warning) => warning.severity !== 'fatal')
    if (rest.some((warning) => warning.severity === 'warn')) {
      await useConfirm({ title: '模型文件预检提示', content: renderWarnings(rest) })
    }
    manifestCheckedKey = key
  }

  /** 最近一次成功探测的基础参数(类别调整后的覆盖重探测复用;local 源为已上传的 staged 路径) */
  let inspectBase: InspectModelPayload | null = null
  /** 已应用的模型类别(与最近一次 inspect 一致;探测预填回写不触发重探测) */
  let appliedType = ''
  /** 重探测序号(防并发串响应:仅采用最新一次结果) */
  let reinspectSeq = 0

  // 来源信息变更后上次探测结果即失效,需重新探测才能继续
  watch(
    () => {
      const model = sourceStep.value?.params()
      return model ? [model.source, model.source_url, model.revision, model.local_mode] : []
    },
    () => {
      inspected.value = null
      feasibility.value = null
      inspectBase = null
      appliedType = ''
    },
  )

  // 模型类别调整后带覆盖重探测:回显 suggested_type 与 feasibility 按所选类别重算
  watch(
    () => infoStep.value?.params().model_type,
    (modelType) => {
      if (!modelType || !inspectBase || modelType === appliedType) return
      void reinspect(modelType)
    },
  )

  /** 覆盖重探测中状态(模型类别变更触发;外网请求较慢,提交按钮置 loading 防并发) */
  const reinspecting = ref(false)

  /** 覆盖重探测(仅类别):仅更新识别回显与可部署性,不回写表单;失败提示由 http 中间件给出,保留旧回显 */
  async function reinspect(modelType: string) {
    const base = inspectBase
    if (!base) return
    appliedType = modelType
    const seq = ++reinspectSeq
    reinspecting.value = true
    try {
      const { files, feasibility: report } = await inspect({ ...base, model_type: modelType })
      if (seq !== reinspectSeq) return
      inspected.value = files
      feasibility.value = report
    } catch {
      // 保留旧回显,用户可再次调整类别重试
    } finally {
      reinspecting.value = false
    }
  }

  /** 探测模型源:成功预填模型信息并自动进入下一步;软失败(success=false)展示原因,停留当前步 */
  async function handleInspect() {
    const step = sourceStep.value
    if (!step) return
    try {
      await step.validate()
    } catch {
      return
    }
    inspecting.value = true
    try {
      const source = step.params()
      // local 源分两种获取方式:upload=先预检文件清单(validate-manifest)再分片上传,staged 目录路径写回来源地址;
      // path=用户直接输入 manager 本机路径,无需上传,source_url 即输入值;
      // 预检/上传失败提示由 http 中间件或预检编排给出,此处中断即可
      if (source.source === 'local' && source.local_mode === 'upload') {
        const localFiles = step.files()
        if (!localFiles.length) {
          message.warning('请先选择要上传的模型文件夹')
          return
        }
        try {
          await ensureManifestChecked(localFiles)
          source.source_url = await upload.ensureUploaded(localFiles, step.folderName())
        } catch {
          return
        }
      }
      const payload: InspectModelPayload = {
        source: source.source,
        source_url: source.source_url,
        // revision 仅 HF 源有意义,空值按后端默认 main 处理
        revision: source.source === 'huggingface' ? source.revision || null : null,
      }
      const { files, feasibility: report } = await inspect(payload)
      inspected.value = files
      feasibility.value = report
      if (!files.success) return
      // local 源的 source_url 已被改写为 staged 路径,payload 即覆盖重探测要复用的基础参数
      inspectBase = payload
      const info = infoStep.value?.params()
      if (info) {
        if (
          files.suggested_type &&
          MODEL_TYPE_OPTIONS.some((option) => option.value === files.suggested_type)
        ) {
          info.model_type = files.suggested_type
        }
        // 名称为空时取 repo/路径最后一段兜底;仅去尾部的纯字母扩展名(本地文件夹常带 .safetensors 等后缀),
        // 含数字/横杠的段(如 Qwen3.8-27B-FP8)属于型号名,必须保留
        if (!info.name) {
          const baseName = source.source_url.replace(/\/+$/, '').split('/').pop() ?? ''
          info.name = baseName.replace(/\.[A-Za-z]+$/, '') || baseName
        }
        info.format = files.format
        info.architecture = files.architecture ?? ''
        info.size_gb = files.size_gb
        info.quantization = files.quantization
        info.is_moe = files.is_moe
      }
      // 预填回写与最近一次探测一致,不应触发覆盖重探测
      appliedType = info?.model_type ?? ''
      message.success('探测成功,已回显识别结果')
      current.value = 2
    } finally {
      inspecting.value = false
    }
  }

  /** 上一步 */
  function prev() {
    if (current.value > 1) current.value -= 1
  }

  /** 提交中状态(防重复点击) */
  const submitting = ref(false)

  /** 确认提交:步骤 2 的人工修正写回识别结果,随创建接口回传;重探测中禁止提交(feasibility 未刷新) */
  async function submit() {
    if (submitting.value || reinspecting.value) return
    const files = inspected.value
    const source = sourceStep.value?.params()
    const info = infoStep.value?.params()
    if (!files?.success || !source || !info) return
    try {
      await infoStep.value?.validate()
    } catch {
      return
    }
    submitting.value = true
    try {
      const mergedFiles: ModelFiles = {
        ...files,
        format: info.format,
        architecture: info.architecture || null,
        size_gb: info.size_gb,
        quantization: info.quantization,
        is_moe: info.is_moe,
      }
      const created = await create(
        {
          name: info.name,
          source: source.source,
          source_url: source.source_url,
          revision: source.source === 'huggingface' ? source.revision : '',
          model_type: info.model_type,
        },
        mergedFiles,
      )
      message.success(`模型 ${info.name} 已创建`)
      onCreated?.(created)
      close()
    } finally {
      submitting.value = false
    }
  }

  return {
    STEPS,
    current,
    sourceStep,
    infoStep,
    inspecting,
    reinspecting,
    inspected,
    feasibility,
    submitting,
    manifestWarnings,
    uploadState: upload.state,
    close,
    handleInspect,
    prev,
    submit,
    /** 文件清单变化后旧的预检标记即失效 */
    clearManifestWarnings: () => {
      manifestWarnings.value = []
      manifestCheckedKey = ''
    },
  }
}
