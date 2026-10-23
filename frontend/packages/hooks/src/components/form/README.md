# useForm

配置化表单 Hook，声明 `data()` + `options()`，返回可直接渲染的 `LmForm` 与操作方法。

## 使用

```vue
<script setup lang="ts">
import { FormInput, FormSelect, FormSwitch, FormButton, useForm } from '@repo/hooks'

const { LmForm, submit, submitting } = useForm({
  data: () => ({ name: '', type: 'a', enabled: true }),
  options: () => [
    FormInput('名称', 'name', {
      rules: [{ required: true, message: '请输入名称', trigger: 'blur' }],
    }),
    FormSelect('类型', 'type', [
      { label: 'A', value: 'a' },
      { label: 'B', value: 'b' },
    ]),
    FormSwitch('启用', 'enabled'),
    FormButton('保存', {
      attrs: { type: 'primary' },
      loading: submitting,
      onClick: ({ submit }) => submit(),
    }),
  ],
  submit: async (model, validate) => {
    await validate()
    // await api(model)
  },
})
</script>

<template>
  <LmForm />
</template>
```

## 返回值

- `LmForm`：动态组件，`<LmForm />` 直接渲染。
- `formRef` / `formOptions` / `submit` / `validate` / `reset` / `params` / `setInitialValues` / `submitting`。
