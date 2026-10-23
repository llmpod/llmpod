<script setup lang="ts">
import { useController } from '../useController'

const { ProfileForm, PasswordForm } = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 主题设置 -->
    <n-card title="主题设置" segmented>
      <LmThemeModeCard class="max-w-[36rem]" />
    </n-card>

    <!-- 个人信息 -->
    <n-card title="个人信息" segmented>
      <ProfileForm class="max-w-[42rem]" />
    </n-card>

    <!-- 修改密码 -->
    <n-card title="修改密码" segmented>
      <PasswordForm class="max-w-[42rem]" />
    </n-card>
  </div>
</template>
