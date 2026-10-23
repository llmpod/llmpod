<script setup lang="ts">
import { useForm, FormInput, FormSelect, useDialogClose, message } from '@repo/hooks'
import { useStore, eventFormOptions, type Webhook, type WebhookEvent } from './useStore'

/** 编辑回填数据;新增时为 undefined */
const props = defineProps<{
  initial?: Webhook
}>()

const close = useDialogClose()
const { save } = useStore()

const { LmForm, submit } = useForm<{
  id?: number
  name: string
  event: WebhookEvent | ''
  url: string
}>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    event: props.initial?.event ?? '',
    url: props.initial?.url ?? '',
  }),
  options: () => [
    FormInput('名称', 'name', {
      rules: [{ required: true, message: '请输入名称', trigger: 'blur' }],
      attrs: { placeholder: '如:模型部署完成' },
    }),
    FormSelect('事件类型', 'event', eventFormOptions, {
      rules: [{ required: true, message: '请选择事件类型', trigger: 'change' }],
      attrs: { placeholder: '请选择事件类型' },
    }),
    FormInput('目标URL', 'url', {
      rules: [{ required: true, message: '请输入目标URL', trigger: 'blur' }],
      attrs: { placeholder: 'https://hook.company.com/deploy' },
    }),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    const { mode, name } = await save(model as Omit<Webhook, 'enabled' | 'lastTriggeredAt'>)
    message.success(mode === 'create' ? `已创建:${name}` : `已保存:${name}`)
    close()
  },
})

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
