<script setup lang="ts">
import { useForm, FormInput, FormInputNumber, FormSelect, useDialogClose, message } from '@repo/hooks'
import { useStore, typeFormOptions, type Package, type PackageType } from './useStore'

/** 编辑回填数据;新增时为 undefined */
const props = defineProps<{
  initial?: Package
}>()

const close = useDialogClose()
const { save } = useStore()

const { LmForm, submit } = useForm<{
  id?: number
  name: string
  type: PackageType | ''
  price: number | null
  tokenQuota: string
  qps: number | null
  concurrency: number | null
}>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    type: props.initial?.type ?? '',
    price: props.initial?.price ?? null,
    tokenQuota: props.initial?.tokenQuota ?? '',
    qps: props.initial?.qps ?? 50,
    concurrency: props.initial?.concurrency ?? 10,
  }),
  options: () => [
    FormInput('名称', 'name', {
      rules: [{ required: true, message: '请输入套餐名称', trigger: 'blur' }],
      attrs: { placeholder: '基础版' },
    }),
    FormSelect('类型', 'type', typeFormOptions, {
      rules: [{ required: true, message: '请选择套餐类型', trigger: 'change' }],
      attrs: { placeholder: '包月' },
    }),
    FormInputNumber('价格', 'price', {
      rules: [{ required: true, type: 'number', message: '请输入价格', trigger: 'blur' }],
      attrs: { min: 0, placeholder: '¥', class: 'w-full' },
    }),
    FormInput('Token额度', 'tokenQuota', {
      rules: [{ required: true, message: '请输入Token额度', trigger: 'blur' }],
      attrs: { placeholder: '如1000万' },
    }),
    FormInputNumber('QPS', 'qps', {
      attrs: { min: 0, placeholder: '50', class: 'w-full' },
    }),
    FormInputNumber('并发数', 'concurrency', {
      attrs: { min: 0, placeholder: '10', class: 'w-full' },
    }),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    // 保存 + 提示 + 关闭均封装在组件内部,不依赖父级回调
    const { mode, name } = await save(model)
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
