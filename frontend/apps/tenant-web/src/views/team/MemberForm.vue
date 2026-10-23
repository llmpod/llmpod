<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useForm, FormInput, FormPassword, FormSelect, useDialogClose, message } from '@repo/hooks'
import { listRoles } from '@/api/access'
import { useStore, type Member } from './useStore'

/** 编辑回填数据;创建时为 undefined */
const props = defineProps<{
  initial?: Member
}>()

const isEdit = !!props.initial

const close = useDialogClose()
const { createMember, updateMember } = useStore()

/** 角色下拉选项(多选,值为角色ID) */
const roleOptions = ref<Array<{ label: string; value: number }>>([])

interface MemberFormModel {
  id?: number
  username: string
  /** 编辑态留空表示不修改密码 */
  password: string
  role_ids: number[]
  email: string
  phone: string
}

const { LmForm, submit } = useForm<MemberFormModel>({
  data: () => ({
    id: props.initial?.id,
    username: props.initial?.username ?? '',
    password: '',
    role_ids: props.initial?.role_ids ?? [],
    email: props.initial?.email ?? '',
    phone: props.initial?.phone ?? '',
  }),
  options: () => [
    FormInput('用户名', 'username', {
      rules: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
      // username 不可改(后端不支持重命名)
      attrs: { placeholder: '全局唯一,登录用', disabled: isEdit },
    }),
    FormPassword('密码', 'password', {
      rules: isEdit ? [] : [{ required: true, message: '请输入密码', trigger: 'blur' }],
      attrs: { placeholder: isEdit ? '留空则不修改密码' : '登录密码' },
    }),
    FormSelect('角色', 'role_ids', roleOptions.value, {
      rules: isEdit
        ? []
        : [{ required: true, type: 'array', message: '请选择角色', trigger: 'change' }],
      attrs: {
        multiple: true,
        filterable: true,
        placeholder: '请选择角色',
        // 编辑态角色不可改(走角色管理页分配/回收)
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
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    if (isEdit) {
      // 更新接口语义:不传则不修改;空串一律转 undefined 保留原值
      await updateMember(model.id!, {
        password: model.password || undefined,
        email: model.email || undefined,
        phone: model.phone || undefined,
      })
      message.success(`已保存:${model.username}`)
    } else {
      // 租户用户接口按会话 tenant_id 归属,body 不再传 tenant_id;角色必须属当前租户
      await createMember({
        username: model.username,
        password: model.password,
        role_ids: model.role_ids.map(Number).filter((n) => Number.isInteger(n)),
        email: model.email || undefined,
        phone: model.phone || undefined,
      })
      message.success(`已创建成员:${model.username}`)
    }
    close()
  },
})

// 弹窗打开时拉取本租户角色作为选项
onMounted(async () => {
  const roles = await listRoles()
  roleOptions.value = roles.map((role) => ({ label: role.name, value: role.id }))
})

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (closeDialog: () => void) => submit().then(closeDialog, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
