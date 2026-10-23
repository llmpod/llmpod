<script setup lang="ts">
import { AddCircleOutline, CloseOutline } from '@vicons/ionicons5'
import { FormInput, FormSelect, FormTextarea, message, useForm } from '@repo/hooks'
import { CLUSTER_TYPE_OPTIONS, useStore, type ClusterFormValues } from './useStore'

/** 创建成功/取消时通知父级(步骤1)收起面板 */
const emit = defineEmits<{ created: []; cancel: [] }>()

const { submitCluster } = useStore()

const { LmForm, submit, submitting } = useForm<ClusterFormValues>({
  data: () => ({
    name: '',
    type: 'docker',
    description: '',
  }),
  options: () => [
    FormInput('集群名称', 'name', {
      rules: [{ required: true, message: '请输入集群名称', trigger: 'blur' }],
      attrs: { placeholder: '如:gpu-cluster' },
    }),
    FormSelect('集群类型', 'type', CLUSTER_TYPE_OPTIONS, {
      attrs: { clearable: false },
    }),
    FormTextarea('描述', 'description', {
      attrs: { placeholder: '请输入描述(可选)', rows: 2 },
    }),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    await validate()
    await submitCluster(model)
    message.success(`集群 ${model.name} 已创建`)
    emit('created')
  },
})

/** 创建:失败(校验/接口)保留表单,错误由字段提示与接口拦截器呈现 */
function onCreate() {
  void submit().catch(() => {})
}
</script>

<template>
  <div>
    <div class="flex items-start justify-between">
      <div class="flex items-center gap-[.8rem]">
        <n-icon
          :component="AddCircleOutline"
          :size="20"
          class="text-primary"
        />
        <div>
          <div class="text-strong font-semibold">
            新建集群
          </div>
          <div class="text-small text-secondary mt-[.2rem]">
            创建后将切换到新集群,节点需重新添加
          </div>
        </div>
      </div>
      <n-button
        quaternary
        circle
        size="small"
        :disabled="submitting"
        @click="emit('cancel')"
      >
        <template #icon>
          <n-icon :component="CloseOutline" />
        </template>
      </n-button>
    </div>

    <div class="max-w-[48rem] mx-auto mt-[1.6rem]">
      <LmForm />
      <div class="flex items-center justify-end gap-[1rem]">
        <n-button
          :disabled="submitting"
          @click="emit('cancel')"
        >
          取消
        </n-button>
        <n-button
          type="primary"
          :loading="submitting"
          @click="onCreate"
        >
          创建集群
        </n-button>
      </div>
    </div>
  </div>
</template>
