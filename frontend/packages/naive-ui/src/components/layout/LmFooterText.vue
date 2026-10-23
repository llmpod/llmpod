<script setup lang="ts">
import { LogoGithub } from '@vicons/ionicons5'

/** 仓库地址写死:github 图标不随页脚文案变化,页脚渲染时恒显 */
const GITHUB_URL = 'https://github.com/llmpod/llmpod'

defineProps<{ text: string }>()

/** 平台版本:后端注入 index.html 的只读附加键(免登录出口);管理员未配置时种子为 null,不展示 */
const serverVersion = window.__LLMPOD_SETTINGS__?.serverVersion ?? ''
</script>

<template>
  <span>
    {{ text }}
    <a
      :href="GITHUB_URL"
      target="_blank"
      rel="noopener noreferrer"
      class="ml-[.6rem] inline-flex items-center align-middle text-tertiary transition-colors hover:text-default"
    >
      <n-icon :size="17">
        <LogoGithub />
      </n-icon>
    </a>
    <span
      v-if="serverVersion"
      class="ml-[.6rem] tabular-nums"
    >v{{ serverVersion }}</span>
  </span>
</template>
