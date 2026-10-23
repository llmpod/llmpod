<script setup lang="ts">
import { useController } from './useController'

const { models, sender, initialModel } = useController()
</script>

<template>
  <div class="playground-page">
    <lm-playground :models="models" :sender="sender" :initial-model="initialModel" />
  </div>
</template>

<style scoped lang="scss">
.playground-page {
  height: 100%;
  background: var(--c-page);
}
</style>
