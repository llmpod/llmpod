<script setup lang="ts">
import { NaiveProvider } from '@repo/naive-ui'
import { useTheme, DialogHost, MessageHost } from '@repo/hooks'

const { theme, themeOverrides } = useTheme()
const isDev = import.meta.env.DEV
</script>

<template>
  <NaiveProvider :theme="theme" :theme-overrides="themeOverrides">
    <RouterView />
    <DialogHost />
    <MessageHost />
  </NaiveProvider>
</template>
