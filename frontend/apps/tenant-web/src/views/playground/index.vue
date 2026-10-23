<script setup lang="ts">
import { useController } from './useController'

const { models, apiKeys, apiKey, sender, openCreateApiKey, initialModel } = useController()
</script>

<template>
  <div class="playground-page">
    <lm-playground
      v-model:api-key="apiKey"
      :models="models"
      :initial-model="initialModel"
      :api-keys="apiKeys"
      :require-api-key="true"
      :sender="sender"
    >
      <template #api-key-empty>
        <n-button size="small" type="primary" @click="openCreateApiKey">
          创建 API Key
        </n-button>
      </template>
    </lm-playground>
  </div>
</template>

<style scoped lang="scss">
.playground-page {
  height: 100%;
  background: var(--c-page);
}
</style>
