<script setup lang="ts">
import { useController } from './useController'

const { SsoForm, LdapForm } = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <LmCard title="基本信息" segmented>
      <SsoForm class="pt-[.5rem]" />
    </LmCard>
    <LmCard title="LDAP 配置" segmented>
      <LdapForm class="pt-[.5rem]" />
    </LmCard>
  </div>
</template>
