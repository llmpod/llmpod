<script setup lang="ts">
import TelemetrySettings from './TelemetrySettings.vue'
import { useController } from '../useController'

const { LmForm, MaintenanceForm } = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard title="平台信息" segmented>
      <LmForm class="pt-[.5rem]" />
    </LmCard>
    <LmCard title="维护模式" segmented>
      <MaintenanceForm class="pt-[.5rem]" />
    </LmCard>
    <LmCard title="数据与隐私" segmented>
      <TelemetrySettings />
    </LmCard>
  </div>
</template>
