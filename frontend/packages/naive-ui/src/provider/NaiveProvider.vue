<script setup lang="ts">
import {
  NConfigProvider,
  NLoadingBarProvider,
  NDialogProvider,
  NNotificationProvider,
  NMessageProvider,
  dateZhCN,
  zhCN,
  type GlobalTheme,
  type GlobalThemeOverrides,
} from 'naive-ui'

interface Props {
  theme?: GlobalTheme | null
  themeOverrides?: GlobalThemeOverrides
}

withDefaults(defineProps<Props>(), {
  theme: null,
  themeOverrides: () => ({}),
})
</script>

<template>
  <n-config-provider
    :theme="theme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-loading-bar-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <n-message-provider>
            <slot />
          </n-message-provider>
        </n-notification-provider>
      </n-dialog-provider>
    </n-loading-bar-provider>
  </n-config-provider>
</template>
