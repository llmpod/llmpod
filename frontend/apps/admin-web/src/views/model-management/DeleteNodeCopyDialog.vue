<script setup lang="ts">
import { FormRadioGroup, message, useDialogClose, useForm, type UseFormReturn } from '@repo/hooks'
import { useStore, type ModelNodeCopy } from './useStore'

/** 待清理的节点副本 */
const props = defineProps<{ modelId: number; node: ModelNodeCopy }>()

const close = useDialogClose()
const { removeNodeCopy } = useStore()

/** 清理方式:false=仅移除平台记录(磁盘保留,默认);true=删除节点上的模型目录并移除记录 */
const CLEANUP_OPTIONS: { label: string; value: boolean }[] = [
  { label: '仅移除记录(磁盘文件保留)', value: false },
  { label: '删除文件并移除记录', value: true },
]

const form: UseFormReturn<{ delete_files: boolean }> = useForm<{ delete_files: boolean }>({
  data: () => ({ delete_files: false }),
  options: () => [FormRadioGroup('清理方式', 'delete_files', CLEANUP_OPTIONS)],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    await validate()
    await removeNodeCopy(props.modelId, props.node.node_id, model.delete_files)
    message.success(model.delete_files ? '已删除节点文件并移除记录' : '已移除平台记录')
    close()
  },
})

const { LmForm, submit } = form

/** 节点展示名:主机名(IP),缺失回退节点 ID */
const nodeLabel = computed(() => {
  const { hostname, ip, node_id } = props.node
  const name = hostname || node_id
  return ip ? `${name} (${ip})` : name
})

/** 删除磁盘文件是高危动作:选中时在弹窗内给出提示 */
const deleteFiles = computed(() => form.params().delete_files)

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <div class="flex flex-col gap-[1rem] pt-[.5rem]">
    <div class="flex flex-col gap-[.4rem] text-small">
      <div class="flex gap-[.8rem]">
        <span class="shrink-0 w-[4.8rem] text-right text-secondary">节点:</span>
        <span class="text-strong break-all">{{ nodeLabel }}</span>
      </div>
      <div class="flex gap-[.8rem]">
        <span class="shrink-0 w-[4.8rem] text-right text-secondary">路径:</span>
        <span class="font-mono break-all">{{ node.path || '--' }}</span>
      </div>
    </div>
    <LmForm />
    <n-alert v-if="deleteFiles" type="warning" :show-icon="false" class="text-small">
      将删除该节点上「{{ node.path || nodeLabel }}」目录下的模型文件,不可恢复。
    </n-alert>
  </div>
</template>