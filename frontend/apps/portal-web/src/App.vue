<script setup lang="ts">
import { NaiveProvider } from '@repo/naive-ui'
import { useTheme, MessageHost, DialogHost } from '@repo/hooks'

const { theme, themeOverrides } = useTheme()
</script>

<template>
  <NaiveProvider :theme="theme" :theme-overrides="themeOverrides">
    <RouterView />
    <MessageHost />
    <DialogHost />
  </NaiveProvider>
</template>
