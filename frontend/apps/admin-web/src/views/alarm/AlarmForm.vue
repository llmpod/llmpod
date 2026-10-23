<script setup lang="ts">
import { useForm, FormInput, FormInputNumber, FormSelect, useDialogClose, message } from '@repo/hooks'
import {
  useStore,
  metricFormOptions,
  comparatorFormOptions,
  channelFormOptions,
  type AlarmRule,
  type AlarmMetric,
  type AlarmChannel,
} from './useStore'

/** 编辑回填数据;新增时为 undefined */
const props = defineProps<{
  initial?: AlarmRule
}>()

const close = useDialogClose()
const { save } = useStore()

const { LmForm, submit } = useForm<{
  id?: number
  name: string
  metric: AlarmMetric | ''
  comparator: '<' | '>'
  threshold: number | null
  channels: AlarmChannel[]
}>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    metric: props.initial?.metric ?? '',
    comparator: props.initial?.comparator ?? '<',
    threshold: props.initial?.threshold ?? null,
    channels: props.initial?.channels ?? ['email'],
  }),
  options: () => [
    FormInput('规则名称', 'name', {
      rules: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
      attrs: { placeholder: '如:月度预算超限' },
    }),
    FormSelect('监控指标', 'metric', metricFormOptions, {
      rules: [{ required: true, message: '请选择监控指标', trigger: 'change' }],
      attrs: { placeholder: '请选择监控指标' },
    }),
    FormSelect('比较方式', 'comparator', comparatorFormOptions, {
      attrs: { clearable: false },
    }),
    FormInputNumber('阈值(元)', 'threshold', {
      rules: [{ required: true, type: 'number', message: '请输入阈值', trigger: 'blur' }],
      attrs: { min: 0, placeholder: '¥100', class: 'w-full' },
    }),
    FormSelect('通知方式', 'channels', channelFormOptions, {
      rules: [{ required: true, type: 'array', message: '请选择通知方式', trigger: 'change' }],
      attrs: { multiple: true, placeholder: '邮件' },
    }),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    const { mode, name } = await save(model as Omit<AlarmRule, 'enabled'>)
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
