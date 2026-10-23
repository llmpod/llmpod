<script setup lang="ts">
import { h, onMounted } from 'vue'
import { NTree } from 'naive-ui'
import {
  useForm,
  FormInput,
  FormCustom,
  useDialogClose,
  message,
} from '@repo/hooks'
import { permissionTreeData, useStore, type RoleData } from './useStore'

/** 编辑回填数据;新增时为 undefined */
const props = defineProps<{
  initial?: RoleData
}>()

const isEdit = !!props.initial

const close = useDialogClose()
const { save, loadPermissions } = useStore()

interface RoleFormModel {
  id?: number
  name: string
  /** 勾选的权限ID集合(树叶子节点) */
  permission_ids: number[]
}

const { LmForm, submit } = useForm<RoleFormModel>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    permission_ids: [],
  }),
  options: () => [
    FormInput('角色名称', 'name', {
      rules: [{ required: true, message: '请输入角色名称', trigger: 'blur' }],
      attrs: { placeholder: '例如:安全审计员' },
    }),
    // 平台域角色固定 tenant_id=0,无所属租户字段;权限编辑在权限抽屉中也可进行,编辑模式仅支持改名
    ...(!isEdit
      ? [
          FormCustom(
            (ctx) => {
              const model = ctx.model as RoleFormModel
              return h(
                'div',
                {
                  class: 'max-h-[40rem] w-full overflow-auto p-[.5rem]',
                },
                [
                  h(NTree, {
                    data: permissionTreeData.value,
                    checkable: true,
                    cascade: true,
                    selectable: false,
                    blockLine: true,
                    defaultExpandAll: true,
                    checkedKeys: model.permission_ids,
                    'onUpdate:checkedKeys': (keys: Array<string | number>) => {
                      // 分组节点 key 为字符串,只保留权限叶子ID
                      model.permission_ids = keys.filter((k): k is number => typeof k === 'number')
                    },
                  }),
                ],
              )
            },
            {
              wrapFormItem: true,
              label: '权限配置(可后续在权限抽屉中调整)',
              field: 'permission_ids',
            },
          ),
        ]
      : []),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    // 保存 + 提示 + 关闭均封装在组件内部,不依赖父级回调
    const { mode, name } = await save({
      id: model.id,
      name: model.name,
      permission_ids: isEdit ? [] : model.permission_ids,
    })
    message.success(mode === 'create' ? `已创建:${name}` : `已保存:${name}`)
    close()
  },
})

// 新增模式预载权限清单供树展示(编辑模式不展示权限配置)
onMounted(async () => {
  if (!isEdit) void loadPermissions()
})

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
