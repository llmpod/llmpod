<script setup lang="ts">
import { computed, h } from 'vue'
import { NTag } from 'naive-ui'
import {
  FormInput,
  FormSelect,
  message,
  useDialogClose,
  useForm,
  type UseFormReturn,
} from '@repo/hooks'
import {
  MODEL_TYPE_OPTIONS,
  QUANTIZATION_OPTIONS,
  SOURCE_LABEL,
  useStore,
  type ModelFormValues,
  type ModelRecord,
  type ModelSource,
} from './useStore'

/** 编辑回填数据 */
const props = defineProps<{ initial: ModelRecord }>()

const close = useDialogClose()
const { update } = useStore()

// 显式标注类型:避免 options 闭包与表单类型循环推断报错
const form: UseFormReturn<ModelFormValues> = useForm<ModelFormValues>({
  data: () => ({
    id: props.initial.id,
    name: props.initial.name,
    // 仅随提交回传,不展示不编辑
    source: (props.initial.source as ModelSource | null) ?? 'huggingface',
    source_url: props.initial.source_url ?? '',
    revision: props.initial.revision ?? '',
    model_type: props.initial.model_type,
    // 不展示不编辑,经名称项 feedback 插槽以 tag 形式只读回显
    format: props.initial.format,
    architecture: props.initial.architecture ?? '',
    size_gb: props.initial.size_gb,
    quantization: props.initial.quantization,
    is_moe: props.initial.is_moe,
  }),
  options: () => [
    FormInput('模型名称', 'name', {
      rules: [{ required: true, message: '请输入模型名称', trigger: 'blur' }],
      attrs: { placeholder: '请输入模型显示名' },
      // 元数据回显挂名称项底部(feedback 插槽,无校验时为空状态),随表单项缩进,不另起分组
      slots: { feedback: () => renderMetaEcho() },
    }),
    FormSelect('模型类别', 'model_type', MODEL_TYPE_OPTIONS, {
      rules: [{ required: true, message: '请选择模型类别', trigger: 'change' }],
      attrs: { clearable: false },
    }),
    FormSelect('量化方案', 'quantization', QUANTIZATION_OPTIONS, {
      attrs: { placeholder: '未识别' },
    }),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    await update(model)
    message.success(`模型 ${model.name} 已更新`)
    close()
  },
})

const { LmForm, submit, params } = form

/** 来源信息:模型源 + 地址/版本,只读展示,不参与表单 */
const sourceInfo = computed(() => {
  const { source, source_url, revision } = params()
  return {
    label: SOURCE_LABEL[source] ?? source,
    url: source_url,
    revision: revision || '',
  }
})

/**
 * 元数据回显:权重格式/架构/大小/MoE,只读 tag,无文字 label。
 * 名称必填校验未通过(为空)时返回 null,把 feedback 区让给校验错误文案;
 * naive-ui 的 feedback 插槽会整体覆盖 renderExplains,不主动让位则错误信息无法显示。
 * 注:当前 name 仅 required 规则,故以 name 为空近似校验失败;若后续新增格式校验需另行处理。
 */
function renderMetaEcho() {
  const { name, format, architecture, size_gb, is_moe } = params()
  const tags = [
    format && `${format.toUpperCase()} 格式`,
    architecture,
    size_gb != null && `${size_gb}GB`,
    is_moe && 'MoE',
  ].filter(Boolean) as string[]
  if (!name.trim() || !tags.length) return null
  return h(
    'div',
    { class: 'meta-echo' },
    tags.map((text) =>
      h(
        NTag,
        { key: text, size: 'tiny', type: 'info', bordered: false, class: 'mr-[1rem]' },
        { default: () => text },
      ),
    ),
  )
}

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <div class="flex flex-col gap-[.75rem] pt-[.75rem]">
    <LmForm />
  </div>
</template>

<style scoped>
/* 来源信息:固定 label 列宽,与表单左对齐 */
.source-label {
  width: 6.4rem;
}

/* 元数据回显:多枚 tiny tag 并列,随表单项缩进,视觉层级低于表单控件 */
.meta-echo {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  padding-top: 0.4rem;
}
</style>
