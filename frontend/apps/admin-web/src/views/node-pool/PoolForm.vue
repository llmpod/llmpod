<script setup lang="tsx">
import { FormInput, FormSelect, message, useDialogClose, useForm } from '@repo/hooks'
import {
  CLUSTER_TYPE_OPTIONS,
  useStore,
  type NodePoolFormValues,
  type NodePoolRecord,
} from './useStore'

/** 编辑回填数据;新建时为 undefined */
const props = defineProps<{ initial?: NodePoolRecord }>()

const close = useDialogClose()
const { save } = useStore()

const { LmForm, submit } = useForm<NodePoolFormValues>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    type: props.initial?.type ?? 'docker',
    description: props.initial?.description ?? '',
  }),
  options: () => [
    FormInput('集群名称', 'name', {
      rules: [{ required: true, message: '请输入集群名称', trigger: 'blur' }],
      attrs: { placeholder: '请输入集群名称' },
    }),
    FormSelect('集群类型', 'type', CLUSTER_TYPE_OPTIONS, {
      // 更新接口不支持修改类型,编辑时禁用
      attrs: { clearable: false, disabled: !!props.initial },
    }),
    FormInput('描述', 'description', {
      attrs: { type: 'textarea', placeholder: '请输入描述(可选)' },
    }),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    // 保存 + 提示 + 关闭均封装在组件内部,不依赖父级回调
    const { mode, name } = await save(model)
    message.success(mode === 'create' ? `集群 ${name} 已创建` : `集群 ${name} 已更新`)
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
