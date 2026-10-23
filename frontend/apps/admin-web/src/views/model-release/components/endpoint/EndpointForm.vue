<script setup lang="ts">
import { computed } from 'vue'
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
  type EndpointFormValues,
  type EndpointData,
} from './useStore'

const props = defineProps<{
  /** 编辑回填；新增时为 undefined */
  initial?: EndpointData
}>()

const close = useDialogClose()
const { create, update } = useStore()
const isEdit = computed(() => !!props.initial?.id)

const { LmForm, submit, setInitialValues } = useForm<EndpointFormValues>({
  data: () => ({
    id: props.initial?.id,
    protocol: props.initial?.protocol ?? 'openai',
    api_base: props.initial?.api_base ?? '',
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
    FormInput('端点名称', 'name', {
      rules: [{ required: true, message: '请输入端点名称', trigger: 'blur' }],
      attrs: { placeholder: '如 OpenAI GPT-4o', maxlength: 255 },
    }),
    FormInput('转发基址', 'api_base', {
      rules: [
        { required: true, message: '请输入转发基址', trigger: 'blur' },
      ],
      attrs: { placeholder: '如 https://api.openai.com/v1', maxlength: 512 },
    }),
    FormInput('上游模型名', 'served_model', {
      rules: [{ required: true, message: '请输入上游模型名', trigger: 'blur' }],
      attrs: { placeholder: '如 gpt-4o', maxlength: 255 },
    }),
    FormInput('外部 API Key', 'api_key', {
      attrs: {
        placeholder: isEdit.value ? '不填=不修改' : '本地端点可空',
        type: 'password',
        showPasswordOn: 'click',
        maxlength: 255,
      },
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
      await create(model)
      message.success(`已创建：${model.name}`)
    }
    close()
  },
})

// 编辑回填：同步 reset 基准
if (props.initial) {
  setInitialValues({
    id: props.initial.id,
    protocol: props.initial.protocol,
    api_base: props.initial.api_base,
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
