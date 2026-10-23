<script setup lang="ts">
import { useController } from './useController'

const { LmTableGroup, LmTable, LmPage, LmRefresh, LmColumn } = useController()
</script>

<template>
  <LmCard>
    <LmTableGroup>
      <template #header-right>
        <LmRefresh />
        <LmColumn />
      </template>
      <template #content>
        <LmTable />
      </template>
      <template #footer-right>
        <LmPage />
      </template>
    </LmTableGroup>
  </LmCard>
</template>
