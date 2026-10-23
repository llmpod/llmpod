<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useForm, FormInput, FormPassword, FormSelect, useDialogClose, message } from '@repo/hooks'
import { listRoles } from '@/api/access'
import {
  useStore,
  userStatusFormOptions,
  type UserData,
  type UserStatus,
} from './useStore'

/** 编辑回填数据;新增时为 undefined */
const props = defineProps<{
  initial?: UserData
}>()

const isEdit = !!props.initial

const close = useDialogClose()
const { save } = useStore()

/** 角色下拉选项(平台域角色,tenant_id=0) */
const roleOptions = ref<Array<{ label: string; value: number }>>([])

const { LmForm, submit } = useForm<{
  id?: number
  username: string
  password: string
  /** 角色 ID 列表(多选下拉,值为数字;必须为平台域角色) */
  role_ids: number[]
  email: string
  phone: string
  status: UserStatus
}>({
  data: () => ({
    id: props.initial?.id,
    username: props.initial?.username ?? '',
    password: '',
    role_ids: props.initial?.role_ids ?? [],
    email: props.initial?.email ?? '',
    phone: props.initial?.phone ?? '',
    status: props.initial?.status ?? 'active',
  }),
  options: () => [
    FormInput('用户名', 'username', {
      rules: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
      // username 不可改(后端不支持重命名)
      attrs: { placeholder: '平台内唯一', disabled: isEdit },
    }),
    FormPassword('密码', 'password', {
      rules: isEdit ? [] : [{ required: true, message: '请输入密码', trigger: 'blur' }],
      attrs: { placeholder: isEdit ? '留空则不修改密码' : '登录密码' },
    }),
    // 角色来自平台域角色清单(GET /api/v1/admin/access/roles,固定只返平台域);编辑态角色走 /admin/access 管理
    FormSelect('角色', 'role_ids', roleOptions.value, {
      rules: isEdit
        ? []
        : [{ required: true, type: 'array', message: '请选择角色', trigger: 'change' }],
      attrs: {
        multiple: true,
        filterable: true,
        placeholder: '请选择平台角色',
        disabled: isEdit,
      },
    }),
    FormInput('邮箱', 'email', {
      attrs: { placeholder: '可选' },
      rules: [
        {
          trigger: 'blur',
          validator: (_rule, value: string) =>
            !value || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) || new Error('邮箱格式不正确'),
        },
      ],
    }),
    FormInput('手机号', 'phone', {
      attrs: { placeholder: '可选' },
    }),
    ...(isEdit
      ? [
          FormSelect('状态', 'status', userStatusFormOptions, {
            rules: [{ required: true, message: '请选择状态', trigger: 'change' }],
            attrs: { clearable: false },
          }),
        ]
      : []),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    // 保存 + 提示 + 关闭均封装在组件内部,不依赖父级回调
    const { mode, name } = await save({
      ...model,
      role_ids: model.role_ids.map(Number).filter((n) => Number.isInteger(n)),
    })
    message.success(mode === 'create' ? `已创建:${name}` : `已保存:${name}`)
    close()
  },
})

// 平台用户固定 tenant_id=0,角色清单为平台域(接口固定只返平台域角色)
onMounted(async () => {
  const roles = await listRoles()
  roleOptions.value = roles.map((r) => ({ label: r.name, value: r.id }))
})

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
