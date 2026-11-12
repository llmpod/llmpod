<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  useForm,
  FormInput,
  FormSelect,
  useDialogClose,
  message,
} from '@repo/hooks'
import {
  useStore,
  protocolOptions,
  normalizeModelIds,
  type EndpointFormValues,
  type EndpointData,
} from './useStore'

const props = defineProps<{
  /** 编辑回填；新增时为 undefined */
  initial?: EndpointData
}>()

const close = useDialogClose()
const { create, update, fetchUpstreamModels } = useStore()
const isEdit = computed(() => !!props.initial?.id)

/** 上游模型清单候选（登记前发现，不落库）；拉取失败仍可手动录入 */
const upstreamOptions = ref<{ label: string; value: string }[]>([])
const upstreamLoading = ref(false)
/** 上次拉取是否失败：下拉空态据此改成「请直接输入」的引导文案 */
const upstreamFailed = ref(false)
/** 上次拉取的入参签名（协议+基址+Key）：同一组入参不重复打上游 */
let upstreamSignature = ''

const { LmForm, submit, setValues, setInitialValues, params } = useForm<EndpointFormValues>({
  data: () => ({
    id: props.initial?.id,
    protocol: props.initial?.protocol ?? 'openai',
    api_base: props.initial?.api_base ?? '',
    // 新建：多选上游模型名；编辑：单条 served_model（/{id}/update 契约不变）
    served_models: props.initial ? [props.initial.served_model] : [],
    served_model: props.initial?.served_model ?? '',
    name: props.initial?.name ?? '',
    api_key: '',
    provider_name: props.initial?.provider_name ?? '',
    path: props.initial?.path ?? '',
    operations: props.initial?.operations ?? [],
  }),
  options: () => [
    FormSelect('协议', 'protocol', protocolOptions, {
      rules: [{ required: true, message: '请选择协议', trigger: 'change' }],
      attrs: { disabled: isEdit.value },
    }),
    // 新建不填端点名：后端缺省取 served_model（name 传空串会因 min_length=1 整批 422）
    FormInput('端点名称', 'name', {
      visible: isEdit.value,
      rules: [{ required: true, message: '请输入端点名称', trigger: 'blur' }],
      attrs: { placeholder: '如 OpenAI GPT-4o', maxlength: 255 },
    }),
    FormInput('转发基址', 'api_base', {
      rules: [
        { required: true, message: '请输入转发基址', trigger: 'blur' },
      ],
      attrs: { placeholder: '如 https://api.openai.com/v1', maxlength: 512 },
    }),
    // 拉清单要用到 api_key，故排在模型名之前
    FormInput('外部 API Key', 'api_key', {
      attrs: {
        placeholder: isEdit.value ? '不填=不修改' : '本地端点可空',
        type: 'password',
        showPasswordOn: 'click',
        maxlength: 255,
      },
    }),
    // 新建：上游模型名多选（打开下拉即拉清单，也可直接输入回车逐个添加）
    FormSelect('上游模型名', 'served_models', upstreamOptions.value, {
      visible: !isEdit.value,
      rules: [
        {
          required: true,
          type: 'array',
          message: '请至少填写一个上游模型名',
          trigger: ['blur', 'change'],
        },
      ],
      attrs: {
        placeholder: '选择或输入模型名，回车添加',
        multiple: true,
        filterable: true,
        tag: true,
        maxTagCount: 'responsive',
        loading: upstreamLoading.value,
        // tag 模式回车即新建一项，这里统一去空白/去重（重复 served_model 会被后端整批 422）
        'onUpdate:value': (value: string[]) =>
          setValues({ served_models: normalizeModelIds(value) }),
        // 打开下拉即拉取上游清单（同入参只拉一次）
        onUpdateShow: (show: boolean) => {
          if (show) void loadUpstreamModels()
        },
      },
      controlSlots: {
        // 清单相关反馈全部走空态文案:不弹 message,也不再设下拉底部操作区
        empty: () =>
          h(
            'div',
            { class: 'px-[1rem] py-[1.2rem] text-center' },
            h('span', { class: 'text-small text-tertiary' }, emptyHint()),
          ),
      },
    }),
    // 编辑：仍是单个上游模型名
    FormInput('上游模型名', 'served_model', {
      visible: isEdit.value,
      rules: [{ required: true, message: '请输入上游模型名', trigger: 'blur' }],
      attrs: { placeholder: '如 gpt-4o', maxlength: 255 },
    }),
    FormInput('供应商名称', 'provider_name', {
      // 暂无实际用途,需要时移除 visible 即可
      visible: false,
      attrs: { placeholder: '前端分组用，可空', maxlength: 100 },
    }),
    FormInput('子路径', 'path', {
      // 暂无实际用途,需要时移除 visible 即可
      visible: false,
      attrs: { placeholder: '不含 /v1，如 chat/completions；空=按协议约定', maxlength: 255 },
    }),
    FormSelect('支持操作', 'operations', [], {
      // 暂不开放编辑,需要时移除 visible 即可
      visible: false,
      attrs: {
        placeholder: '输入操作名，回车添加',
        multiple: true,
        filterable: true,
        tag: true,
        maxTagCount: 'responsive',
      },
    }),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    await validate()
    if (isEdit.value) {
      const { name } = await update(model)
      message.success(`已更新：${name}`)
    } else {
      const names = (await create(model)).map((item) => item.name)
      // 批量登记后逐个点名会撑爆提示,超过 3 个只报总数
      message.success(
        names.length > 3
          ? `已创建 ${names.length} 个端点`
          : `已创建：${names.join('、') || '端点'}`,
      )
    }
    close()
  },
})

/**
 * 拉取上游模型清单：api_base 必填才打上游。
 * 同入参（协议+基址+Key）只请求一次，重复开下拉不再打上游；失败同样记签名，
 * 既不弹 message 也不反复重试——用户改基址/Key 后才触发下一次拉取。
 */
async function loadUpstreamModels(): Promise<void> {
  const model = params()
  const apiBase = model.api_base.trim()
  if (!apiBase || upstreamLoading.value) return
  const signature = [model.protocol, apiBase, model.api_key].join('\u0000')
  if (signature === upstreamSignature) return
  upstreamLoading.value = true
  try {
    const ids = await fetchUpstreamModels({
      protocol: model.protocol,
      api_base: apiBase,
      api_key: model.api_key || null,
    })
    upstreamOptions.value = ids.map((id) => ({ label: id, value: id }))
    upstreamFailed.value = false
  } catch {
    upstreamFailed.value = true
    upstreamOptions.value = []
  } finally {
    upstreamSignature = signature
    upstreamLoading.value = false
  }
}

/** 下拉空态文案：按「未填基址 / 拉取中 / 拉取失败 / 可手动输入」给出当下该做什么 */
function emptyHint(): string {
  if (upstreamLoading.value) return '正在拉取上游模型清单…'
  return '可直接输入模型名后回车添加'
}

/**
 * 转发基址 / 外部 Key 变化即作废上次清单：丢弃候选并清签名，
 * 下次打开下拉自动重新拉一次（不在键入过程中打上游）。
 */
watch(
  [() => params().api_base, () => params().api_key],
  () => {
    upstreamSignature = ''
    upstreamOptions.value = []
    upstreamFailed.value = false
  },
)

// 编辑回填：同步 reset 基准
if (props.initial) {
  setInitialValues({
    id: props.initial.id,
    protocol: props.initial.protocol,
    api_base: props.initial.api_base,
    served_models: [props.initial.served_model],
    served_model: props.initial.served_model,
    name: props.initial.name,
    api_key: '',
    provider_name: props.initial.provider_name ?? '',
    path: props.initial.path ?? '',
    operations: [...props.initial.operations],
  })
}

defineExpose({
  onConfirm: (dialogClose: () => void) => submit().then(dialogClose, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
