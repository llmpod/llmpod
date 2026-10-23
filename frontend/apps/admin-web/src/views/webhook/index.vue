<script setup lang="tsx">
import { NTag } from 'naive-ui'
import { useForm, FormInput, FormCustom, FormButton, message } from '@repo/hooks'
import { useController } from './useController'

const { LmTableGroup, LmTable, LmPage, LmRefresh, LmColumn, openCreate } = useController()

interface SignModel {
  secret: string
}

const { LmForm: SignForm } = useForm<SignModel>({
  data: () => ({
    secret: 'whsec_xxxxxxxxxxxxxxxxxxxx',
  }),
  options: () => [
    FormInput('签名密钥', 'secret', {
      attrs: { class: 'max-w-[440px]', readonly: true },
      itemProps: { feedback: '用于验证 Webhook 请求的合法性' },
    }),
    FormCustom(
      () => (
        <div class="flex items-center gap-[1rem]">
          <span class="text-strong">重试策略</span>
          <NTag type="success" size="small" bordered={false}>
            最多 3 次 · 指数退避
          </NTag>
        </div>
      ),
      {
        wrapFormItem: true,
        itemProps: { feedback: '投递失败时最多重试 3 次,间隔递增' },
      },
    ),
  ],
  submit: async (_, validate) => {
    await validate()
    // 静态页:暂无配置接口,仅校验并提示
    message.success('配置已保存')
  },
})
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard>
      <LmTableGroup>
        <template #header-left>
          <n-button type="primary" @click="openCreate"> 新建Webhook </n-button>
        </template>
        <template #header-right>
          <LmRefresh />
          <LmColumn />
        </template>
        <template #content>
          <LmTable>
            <template #empty>
              <LmTableEmpty
                description="还没有 Webhook,新建一个开始使用"
                action-text="新建Webhook"
                @action="openCreate"
              />
            </template>
          </LmTable>
        </template>
        <template #footer-right>
          <LmPage />
        </template>
      </LmTableGroup>
    </LmCard>
    <LmCard title="签名配置" segmented>
      <SignForm class="pt-[.5rem]" />
    </LmCard>
  </div>
</template>
