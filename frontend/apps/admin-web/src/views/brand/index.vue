<script setup lang="ts">
import { useController } from './useController'

const { BrandForm, LoginPageForm } = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard title="主题模式" segmented>
      <LmThemeModeCard class="max-w-[36rem] pt-[.5rem]" />
    </LmCard>
    <LmCard title="基本品牌信息" segmented>
      <BrandForm class="pt-[.5rem]" />
    </LmCard>
    <LmCard title="登录页定制" segmented>
      <LoginPageForm class="pt-[.5rem]" />
    </LmCard>
  </div>
</template>
