<script setup lang="ts">
import { useForm, FormPassword, useDialogClose, message, goToPortalLogin } from '@repo/hooks'
import { useAuthStore } from '@/store/auth'
import { changePassword } from '@/api/auth'

interface ChangePasswordFormValues {
  old_password: string
  new_password: string
}

/** 改密成功后滞留时长:先让 toast 可见,再清 token 整页跳登录 */
const REDIRECT_DELAY_MS = 1200

const close = useDialogClose()
const auth = useAuthStore()

const { LmForm, submit } = useForm<ChangePasswordFormValues>({
  data: () => ({
    old_password: '',
    new_password: '',
  }),
  options: () => [
    FormPassword('当前密码', 'old_password', {
      rules: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
      attrs: { placeholder: '输入当前密码', showPasswordOn: 'click' },
    }),
    FormPassword('新密码', 'new_password', {
      rules: [{ required: true, message: '请输入新密码', trigger: 'blur' }],
      attrs: { placeholder: '输入新密码', showPasswordOn: 'click' },
    }),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    // 旧密错误由 HTTP 层提示(401 走普通错误反馈),弹窗保留
    await changePassword({
      old_password: model.old_password,
      new_password: model.new_password,
    })
    message.success('密码修改成功,请重新登录')
    close()
    // 成功后当前 token 立即失效:先 toast 再清 token 跳登录
    setTimeout(() => {
      auth.clearToken()
      goToPortalLogin()
    }, REDIRECT_DELAY_MS)
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
